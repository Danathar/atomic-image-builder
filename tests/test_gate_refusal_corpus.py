"""Run the fleet's shared refusal corpus against this repository's Bash gate.

Six repositories each carry their own copy of the PreToolUse hook that keeps
allow-listed commands such as `git diff` and `gh pr view` from reading `.env`
or writing a file. The copies are separate code, so a bypass fixed in one
repository says nothing about the other five (#609).
`tests/fixtures/gate-refusal-corpus.json` is the one table they share: each
row is a command, the verdict every gate has to reach, and the command
prefixes the row depends on. A repository runs the rows its own allow list
makes reachable, so a newly found bypass is one new row, and every repository
that allows the command fails until its gate refuses it.

This repository holds the canonical copy. docs/gate-refusal-corpus.md says
how the others take it.

The hook is run the way Claude Code runs it, through the command
`.claude/settings.json` registers, so a registration that stops pointing at
the gate fails here as well.
"""

from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "tests/fixtures/gate-refusal-corpus.json"
SETTINGS = ROOT / ".claude/settings.json"

REFUSE = "refuse"
ALLOW = "allow"
ROW_FIELDS = {"id", "class", "verdict", "requires", "command", "why"}


def allow_prefixes(settings: dict) -> list[str]:
    """The command prefix each wildcard `Bash(...)` allow rule covers.

    The fleet spells a prefix rule three ways (`git diff:*`, `git diff *`,
    `git diff*`); all three cover `git diff` and what follows it. A rule with
    no wildcard allows one exact command and covers no prefix.
    """
    prefixes = []
    for rule in settings.get("permissions", {}).get("allow", []):
        if not (rule.startswith("Bash(") and rule.endswith(")")):
            continue
        body = rule[len("Bash(") : -1]
        for suffix in (":*", " *", "*"):
            if body.endswith(suffix):
                prefixes.append(body[: -len(suffix)])
                break
    return prefixes


def covered(prefix: str, prefixes: list[str]) -> bool:
    return any(prefix == rule or prefix.startswith(rule + " ") for rule in prefixes)


def applies(row: dict, prefixes: list[str]) -> bool:
    return all(covered(prefix, prefixes) for prefix in row["requires"])


def hook_command(settings: dict) -> str:
    for entry in settings["hooks"]["PreToolUse"]:
        if entry.get("matcher") == "Bash":
            return entry["hooks"][0]["command"]
    raise AssertionError(".claude/settings.json registers no PreToolUse hook for Bash")


def decide(command: str, hook: str) -> tuple[str, subprocess.CompletedProcess]:
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    result = subprocess.run(
        ["bash", "-c", hook],
        input=payload,
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(ROOT)},
        check=False,
    )
    refused = result.returncode == 2 or '"deny"' in result.stdout
    return (REFUSE if refused else ALLOW), result


class CorpusShapeTests(unittest.TestCase):
    """The table is read by gates in two languages, so its shape is pinned."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.corpus = json.loads(CORPUS.read_text(encoding="utf-8"))

    def test_the_schema_version_is_one_this_file_reads(self) -> None:
        self.assertEqual(self.corpus["schema"], 1)

    def test_every_row_has_exactly_the_documented_fields(self) -> None:
        for row in self.corpus["rows"]:
            with self.subTest(row=row.get("id")):
                self.assertEqual(set(row), ROW_FIELDS)
                self.assertIn(row["verdict"], (REFUSE, ALLOW))
                self.assertTrue(row["requires"], "a row has to name what it depends on")
                for prefix in row["requires"]:
                    self.assertIsInstance(prefix, str)
                    self.assertEqual(prefix, prefix.strip())
                self.assertTrue(row["command"].strip())
                self.assertTrue(row["why"].strip())

    def test_row_ids_are_unique(self) -> None:
        ids = [row["id"] for row in self.corpus["rows"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_a_row_requires_the_command_it_starts_with(self) -> None:
        # A row whose command is outside every prefix it requires would run
        # in a repository that never allows the command, and fail there for
        # a gate that has nothing to do with it.
        for row in self.corpus["rows"]:
            with self.subTest(row=row["id"]):
                words = row["command"].split()
                self.assertTrue(
                    any(" ".join(words[i : i + len(p.split())]) == p
                        for p in row["requires"] for i in range(len(words))),
                    f"{row['command']!r} names none of {row['requires']}",
                )

    def test_both_verdicts_are_present(self) -> None:
        # Refusals alone would pass a gate that refuses everything, and a
        # gate that refuses everything gets switched off.
        verdicts = {row["verdict"] for row in self.corpus["rows"]}
        self.assertEqual(verdicts, {REFUSE, ALLOW})


class PrefixMatchTests(unittest.TestCase):
    def test_each_fleet_spelling_of_a_prefix_rule_covers_the_prefix(self) -> None:
        for rule in ("Bash(git diff:*)", "Bash(git diff *)", "Bash(git diff*)"):
            with self.subTest(rule=rule):
                prefixes = allow_prefixes({"permissions": {"allow": [rule]}})
                self.assertTrue(covered("git diff", prefixes))

    def test_an_exact_rule_covers_no_prefix(self) -> None:
        prefixes = allow_prefixes({"permissions": {"allow": ["Bash(ruff check)"]}})
        self.assertFalse(covered("ruff check", prefixes))

    def test_a_prefix_is_covered_only_at_a_word_boundary(self) -> None:
        prefixes = allow_prefixes({"permissions": {"allow": ["Bash(gh pr:*)"]}})
        self.assertTrue(covered("gh pr view", prefixes))
        self.assertFalse(covered("gh prx view", prefixes))
        prefixes = allow_prefixes({"permissions": {"allow": ["Bash(gh pr view:*)"]}})
        self.assertFalse(covered("gh pr", prefixes))


class CorpusVerdictTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
        settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
        cls.prefixes = allow_prefixes(settings)
        cls.hook = hook_command(settings)
        cls.rows = [row for row in cls.corpus["rows"] if applies(row, cls.prefixes)]

    def test_enough_rows_apply_here_to_mean_something(self) -> None:
        # If the allow list stopped covering `git diff`, every row would be
        # skipped and the verdict test would pass on nothing.
        self.assertGreaterEqual(len(self.rows), 20)

    def test_every_row_this_repository_allows_is_decided_the_way_it_says(self) -> None:
        for row in self.rows:
            with self.subTest(row=row["id"], command=row["command"]):
                verdict, result = decide(row["command"], self.hook)
                self.assertIn(result.returncode, (0, 2), result.stderr)
                self.assertEqual(
                    verdict,
                    row["verdict"],
                    f"the corpus says {row['verdict']} {row['command']!r}: {row['why']}",
                )



def probe(case: type[unittest.TestCase], name: str, **fixture) -> unittest.TestResult:
    """Run one test of `case` against `fixture` in place of what setUpClass reads."""
    result = unittest.TestResult()
    type("Probe", (case,), fixture)(name).run(result)
    return result


class HarnessContractTests(unittest.TestCase):
    """The rules docs/gate-refusal-corpus.md states, on inputs this repository lacks.

    The other five repositories copy these rules. Here every row's prefixes
    are allowed together, the hook answers only with exit status 2, and the
    table is valid, so the branches below would never run against the real
    files and could be dropped without a failure.
    """

    ROW = {"id": "r", "class": "c", "verdict": REFUSE, "requires": ["git diff"],
           "command": "git diff /dev/null ./.env", "why": "w"}

    def corpus(self, *rows: dict) -> dict:
        return {"schema": 1, "rows": list(rows)}

    def assertProbeFails(self, result: unittest.TestResult) -> None:
        self.assertEqual(result.errors, [])
        self.assertTrue(result.failures, "the guard let a broken input through")

    def test_a_row_runs_only_when_every_prefix_it_requires_is_allowed(self) -> None:
        row = {**self.ROW, "requires": ["git diff", "git status"]}
        self.assertFalse(applies(row, ["git status"]))
        self.assertFalse(applies(row, ["git diff"]))
        self.assertTrue(applies(row, ["git diff", "git status"]))

    def test_only_bash_rules_cover_a_prefix(self) -> None:
        settings = {"permissions": {"allow": ["Read(./docs/*)", "WebFetch(domain:*)"]}}
        self.assertEqual(allow_prefixes(settings), [])

    def test_the_hook_registered_for_bash_is_the_one_run(self) -> None:
        settings = {"hooks": {"PreToolUse": [
            {"matcher": "Edit", "hooks": [{"command": "edit-hook"}]},
            {"matcher": "Bash", "hooks": [{"command": "bash-hook"}]},
        ]}}
        self.assertEqual(hook_command(settings), "bash-hook")
        with self.assertRaises(AssertionError):
            hook_command({"hooks": {"PreToolUse": settings["hooks"]["PreToolUse"][:1]}})

    def test_a_deny_decision_on_stdout_is_a_refusal(self) -> None:
        deny = ('{"hookSpecificOutput": {"hookEventName": "PreToolUse",'
                ' "permissionDecision": "deny"}}')
        verdict, result = decide("git diff", f"cat >/dev/null; printf '%s' '{deny}'")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(verdict, REFUSE)

    def test_exit_status_decides_when_stdout_is_silent(self) -> None:
        self.assertEqual(decide("git diff", "cat >/dev/null; exit 2")[0], REFUSE)
        self.assertEqual(decide("git diff", "cat >/dev/null; exit 0")[0], ALLOW)

    def test_the_hook_reads_the_command_from_a_bash_tool_payload(self) -> None:
        hook = ("python3 -c 'import json,sys; p=json.load(sys.stdin);"
                " sys.exit(2 if p[\"tool_name\"] == \"Bash\""
                " and p[\"tool_input\"][\"command\"] == \"git diff x\" else 0)'")
        self.assertEqual(decide("git diff x", hook)[0], REFUSE)
        self.assertEqual(decide("git diff y", hook)[0], ALLOW)

    def test_a_table_of_refusals_alone_is_rejected(self) -> None:
        self.assertProbeFails(probe(CorpusShapeTests, "test_both_verdicts_are_present",
                                    corpus=self.corpus(self.ROW)))

    def test_a_row_outside_the_prefix_it_requires_is_rejected(self) -> None:
        row = {**self.ROW, "command": "cat .env"}
        self.assertProbeFails(probe(CorpusShapeTests,
                                    "test_a_row_requires_the_command_it_starts_with",
                                    corpus=self.corpus(row)))

    def test_too_few_reachable_rows_is_rejected(self) -> None:
        self.assertProbeFails(probe(CorpusVerdictTests,
                                    "test_enough_rows_apply_here_to_mean_something",
                                    rows=[]))

    def test_a_hook_that_crashes_fails_even_an_allow_row(self) -> None:
        # Exit status 1 is a hook error, which Claude Code does not treat as
        # a refusal, so an allow row would match it on the verdict alone.
        row = {**self.ROW, "verdict": ALLOW, "command": "git diff"}
        self.assertProbeFails(probe(
            CorpusVerdictTests,
            "test_every_row_this_repository_allows_is_decided_the_way_it_says",
            rows=[row], hook="cat >/dev/null; exit 1"))

    def test_the_guards_pass_the_real_table(self) -> None:
        # The probes above fail for the input they were given, not for how
        # a test is run outside its suite.
        corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
        for name in ("test_both_verdicts_are_present",
                     "test_a_row_requires_the_command_it_starts_with"):
            with self.subTest(test=name):
                self.assertTrue(probe(CorpusShapeTests, name, corpus=corpus).wasSuccessful())


if __name__ == "__main__":
    unittest.main()

"""Hold .claude/risk-config.json to docs/risk-tiers.md.

The document is the map of blast radius an agent and a reviewer read; the
config is the same map for a program. Two copies of one table drift unless
something reads both, so this module does: every tier in one has to be in
the other, each tier's path claims have to match literal for literal, and
every command the config names as evidence has to point at something in the
tree. The document's own claims are joined to the tree by
tests/test_risk_tiers_doc.py, whose parsers this reuses rather than
re-implementing; what is checked here is only that the config says what the
document says.

The config lives under .claude/, which .gitignore excludes wholesale and
re-includes file by file. A re-include left off would leave the file on one
contributor's disk and nowhere else, so being tracked is asserted too.
"""

import json
import re
import shlex
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from test_risk_tiers_doc import (  # noqa: E402
    BACKTICKED,
    DOC,
    doc_text,
    literals,
    paths_paragraph,
    tier_sections,
    tiers_naming,
    tracked_paths,
)

CONFIG = ROOT / ".claude/risk-config.json"
CONFIG_RELATIVE = str(CONFIG.relative_to(ROOT))

# The keys a tier entry may carry. `also_state` is Tier 4's "plus stating
# explicitly ..."; a key outside this set is a claim nothing here checks.
TIER_KEYS = {"name", "reaches", "paths", "evidence"}
OPTIONAL_TIER_KEYS = {"also_state"}

TIER_TITLE = re.compile(r"^## Tier (\d+) [-—] (.+)$")

# Evidence commands run under an interpreter or a build tool; the operand
# that has to exist in the tree is the one after it.
INTERPRETERS = {"python3", "podman"}


def config() -> dict:
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def config_tiers() -> dict[int, dict]:
    return {int(key): value for key, value in config()["tiers"].items()}


def evidence_target(command: str) -> str:
    """The path a command exercises: its argv[0], or the file an interpreter runs.

    `python3 -m unittest discover -s tests` exercises `tests`; `python3
    maintenance_audit.py` exercises the script; `podman build ... -f
    Containerfile .` exercises the Containerfile; `tests/e2e/smoke.sh` is its
    own target.
    """
    argv = shlex.split(command)
    if argv[0] not in INTERPRETERS:
        return argv[0]
    if "-m" in argv:
        return argv[argv.index("-s") + 1] if "-s" in argv else argv[argv.index("-m") + 1]
    if "-f" in argv:
        return argv[argv.index("-f") + 1]
    return argv[1]


def tier_titles() -> dict[int, str]:
    """`## Tier N -- <title>` headings, mapped to the title."""
    titles: dict[int, str] = {}
    for line in doc_text().splitlines():
        match = TIER_TITLE.match(line)
        if match:
            titles[int(match.group(1))] = match.group(2).strip()
    return titles


def labelled_paragraph(body: list[str], label: str) -> str:
    """The paragraph of a tier section that opens with `**<label>:**`.

    The general form of paths_paragraph: bounded by the blank line that ends
    it, the label itself left off.
    """
    marker = f"**{label}:**"
    collected: list[str] = []
    for line in body:
        if not collected and line.startswith(marker):
            collected.append(line[len(marker) :])
        elif collected:
            if not line.strip():
                break
            collected.append(line)
    if not collected:
        raise AssertionError(f"a tier section has no {marker} paragraph")
    return " ".join(part.strip() for part in collected).strip()


def prose(text: str) -> str:
    """Prose reduced to what the config can repeat: no code marks, one space,
    one case, and a sentence break written the way the config writes it."""
    text = " ".join(text.replace("`", "").split()).casefold()
    return text.replace(". ", "; ")


def is_build(command: str) -> bool:
    return shlex.split(command)[:2] == ["podman", "build"]


def named_commands(body: list[str]) -> set[str]:
    """Backticked commands a tier section names: a shell script under tests/
    or a python3 invocation. Paths paragraph excluded -- those are claims
    about what the tier covers, not what it runs."""
    paths = paths_paragraph(body)
    text = " ".join(line.strip() for line in body).replace(paths, "")
    return {
        literal
        for literal in BACKTICKED.findall(text)
        if (literal.startswith("tests/") and literal.endswith(".sh")) or literal.startswith("python3 ")
    }


class ConfigShapeTests(unittest.TestCase):
    def test_the_config_is_tracked_despite_the_claude_exclusion(self) -> None:
        # .gitignore says `.claude/*`; without its own `!` line the file is
        # invisible to git and to every test that reads the tracked tree.
        self.assertIn(CONFIG_RELATIVE, tracked_paths())

    def test_the_config_names_the_document_it_copies(self) -> None:
        self.assertEqual(config()["source"], str(DOC.relative_to(ROOT)))

    def test_the_document_names_the_config_and_this_test(self) -> None:
        text = DOC.read_text()
        self.assertIn(f"`{CONFIG_RELATIVE}`", text)
        self.assertIn("tests/test_risk_config.py", text)

    def test_a_spanning_change_takes_the_highest_tier(self) -> None:
        # "A change spanning tiers takes the highest one it touches."
        self.assertIs(config()["highest_tier_wins"], True)
        self.assertIn("takes the highest one it touches", DOC.read_text())


class TierJoinTests(unittest.TestCase):
    def test_the_tier_sets_agree(self) -> None:
        self.assertEqual(sorted(config_tiers()), sorted(tier_sections()))

    def test_each_tier_repeats_the_documents_path_claims_in_order(self) -> None:
        sections = tier_sections()
        for tier, entry in sorted(config_tiers().items()):
            with self.subTest(tier=tier):
                self.assertEqual(entry["paths"], literals(paths_paragraph(sections[tier])))

    def test_each_tier_states_a_name_a_reach_and_evidence(self) -> None:
        for tier, entry in sorted(config_tiers().items()):
            with self.subTest(tier=tier):
                self.assertTrue(entry["name"].strip())
                self.assertTrue(entry["reaches"].strip())
                self.assertTrue(entry["evidence"])

    def test_evidence_accumulates_up_the_tiers(self) -> None:
        # Tier 2 is "the unit suite plus a real build", Tier 4 "everything
        # above, plus ...": a higher tier never asks for less than the one
        # below it.
        tiers = config_tiers()
        for tier in sorted(tiers)[1:]:
            with self.subTest(tier=tier):
                self.assertLessEqual(set(tiers[tier - 1]["evidence"]), set(tiers[tier]["evidence"]))

    def test_every_evidence_command_points_at_something_in_the_tree(self) -> None:
        tracked = tracked_paths()
        for tier, entry in sorted(config_tiers().items()):
            for command in entry["evidence"]:
                target = evidence_target(command)
                with self.subTest(tier=tier, command=command):
                    self.assertTrue(
                        target in tracked or any(path.startswith(target + "/") for path in tracked),
                        f"{command!r} names {target!r}, which is not in the tree",
                    )

    def test_the_lowest_tier_runs_the_unit_suite_and_the_full_audit_sits_above_it(self) -> None:
        # Tier 1: "the unit suite"; Tier 3: "`python3 maintenance_audit.py`
        # (not `--skip-upstream`...)". The tier numbers are what a reader
        # acts on, so the two commands have to sit where the document puts them.
        tiers = config_tiers()
        self.assertIn("python3 -m unittest discover -s tests", tiers[1]["evidence"])
        self.assertNotIn("python3 maintenance_audit.py", tiers[1]["evidence"])
        self.assertIn("python3 maintenance_audit.py", tiers[3]["evidence"])
        for command in tiers[3]["evidence"]:
            self.assertNotIn("--skip-upstream", command)

    def test_the_config_sits_in_tier_one_only(self) -> None:
        # A copy of a Tier 1 document reaches no further than the document.
        self.assertEqual(tiers_naming(CONFIG_RELATIVE), {1})


class TierProseJoinTests(unittest.TestCase):
    """The config's per-tier name, reach and evidence against the document's
    own words for them. The path lists were already joined literal for
    literal; nothing read the rest, so a tier could be renamed, its reach
    cut short or a named suite dropped from its evidence and every test
    above stayed green."""

    def test_each_tier_carries_only_keys_something_checks(self) -> None:
        for tier, entry in sorted(config_tiers().items()):
            with self.subTest(tier=tier):
                self.assertLessEqual(TIER_KEYS, set(entry))
                self.assertLessEqual(set(entry), TIER_KEYS | OPTIONAL_TIER_KEYS)

    def test_each_tier_name_is_the_documents_heading(self) -> None:
        titles = tier_titles()
        for tier, entry in sorted(config_tiers().items()):
            with self.subTest(tier=tier):
                self.assertEqual(entry["name"], titles[tier])

    def test_each_reach_is_the_opening_of_the_documents_reaches_paragraph(self) -> None:
        # The config keeps the reach and drops the explanation after it:
        # Tier 3's "A stale pin becomes ..." and Tier 4's ", which is what
        # `brew upgrade` installs". So the reach has to be where the
        # paragraph starts and stop where a sentence does, or where a
        # non-restrictive "which" clause starts -- not mid-list.
        sections = tier_sections()
        for tier, entry in sorted(config_tiers().items()):
            with self.subTest(tier=tier):
                paragraph = prose(labelled_paragraph(sections[tier], "Reaches"))
                reach = prose(entry["reaches"])
                self.assertTrue(paragraph.startswith(reach), f"{reach!r} does not open {paragraph!r}")
                rest = paragraph[len(reach) :]
                self.assertTrue(
                    rest.startswith((".", ";", ", which ")),
                    f"Tier {tier}'s reach stops mid-sentence, before {rest[:40]!r}",
                )

    def test_every_command_a_tier_section_names_is_in_that_tiers_evidence(self) -> None:
        # Tier 2 names smoke.sh, test_contrib_aib.sh and test_entrypoint.sh as
        # what proves it; Tier 3 names `python3 maintenance_audit.py`. A
        # command the document says a tier runs and the config leaves out is
        # the drift this file exists to catch.
        sections = tier_sections()
        tiers = config_tiers()
        for tier in sorted(tiers):
            for command in sorted(named_commands(sections[tier])):
                with self.subTest(tier=tier, command=command):
                    self.assertIn(command, tiers[tier]["evidence"])

    def test_the_named_commands_are_found_at_all(self) -> None:
        # Guards the reader above: if it found nothing it would pass vacuously.
        sections = tier_sections()
        self.assertEqual(
            named_commands(sections[2]),
            {"tests/e2e/smoke.sh", "tests/test_contrib_aib.sh", "tests/test_entrypoint.sh"},
        )
        self.assertIn("python3 maintenance_audit.py", named_commands(sections[3]))

    def test_tier_one_is_the_unit_suite_and_nothing_else(self) -> None:
        # "**Evidence:** the unit suite." A build or an end-to-end run listed
        # here would tell a documentation change to do work the document says
        # it does not need.
        self.assertIn("the unit suite", labelled_paragraph(tier_sections()[1], "Evidence"))
        self.assertEqual(config_tiers()[1]["evidence"], ["python3 -m unittest discover -s tests"])

    def test_tier_two_adds_the_real_build(self) -> None:
        # "the unit suite plus a real build" -- the build is what Tier 2 adds.
        self.assertIn("plus a real build", labelled_paragraph(tier_sections()[2], "Evidence"))
        self.assertTrue(any(is_build(command) for command in config_tiers()[2]["evidence"]))

    def test_only_tier_four_asks_for_a_statement_and_it_is_the_documents(self) -> None:
        tiers = config_tiers()
        self.assertEqual({tier for tier, entry in tiers.items() if "also_state" in entry}, {4})
        paragraph = prose(labelled_paragraph(tier_sections()[4], "Evidence"))
        statement = "stating explicitly " + prose(tiers[4]["also_state"])
        self.assertIn(statement, paragraph)
        # The whole of what the document asks for, not the first half of it.
        rest = paragraph[paragraph.index(statement) + len(statement) :]
        self.assertTrue(rest.startswith((".", ";")), f"the statement stops before {rest[:40]!r}")


class EvidenceTargetTests(unittest.TestCase):
    """The operand reader is the join for the evidence check; a shape it
    mis-reads would pass an unrunnable command."""

    def test_a_script_is_its_own_target(self) -> None:
        self.assertEqual(evidence_target("tests/e2e/smoke.sh"), "tests/e2e/smoke.sh")

    def test_a_module_run_targets_the_suite_directory(self) -> None:
        self.assertEqual(evidence_target("python3 -m unittest discover -s tests"), "tests")

    def test_a_python_script_targets_the_script(self) -> None:
        self.assertEqual(evidence_target("python3 maintenance_audit.py"), "maintenance_audit.py")

    def test_a_podman_build_targets_the_containerfile(self) -> None:
        self.assertEqual(evidence_target("podman build -t x -f Containerfile ."), "Containerfile")


if __name__ == "__main__":
    unittest.main()

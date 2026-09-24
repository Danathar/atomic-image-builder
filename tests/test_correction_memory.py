"""Join .claude/memory/, the correction record, to the files it describes.

Script: tests/test_correction_memory.py
What: Reads .claude/memory/README.md and .claude/memory/corrections.md as
      subjects: the README's file table and the `.gitignore` rule it depends
      on, the shape every correction entry is written in, the issue and pull
      request each entry cites, and -- one test per entry -- the thing each
      entry says the repository "settled on".
Doing: Splits corrections.md into its `##` entries and classifies every one
       exhaustively against SETTLED_ON, so an entry added without a join here
       fails rather than going unread. Each join reads the setting it names
       out of the workflow, module, script or document that decides it, and
       numbers the entry quotes are read out of the entry's own text.
Why: Two tests opened corrections.md before this one, and neither read it:
     one checks it still exists, the other that it is not a second copy of
     the agent conventions. Every entry is a claim about how the repository
     works today -- "tests/e2e/ is a trigger", "exact versions in the
     workflow", "write array pushes one per line" -- that an agent reads
     first, from a cold start, with no way to check it. `.coveragerc`
     measures Python modules and ruff does not read Markdown, so no
     percentage moves when one of them stops being true.
Goal: Make a correction whose settlement is undone, or a record link that
      points at the wrong thing, fail here.

The behaviour each settlement protects is not re-tested: the drift threshold,
the pin in CONTRIBUTING.md and the gate-line scan each have their own tests
already. This module checks that the record still describes them.
"""

from __future__ import annotations

import json
import re
import subprocess
import unittest
from pathlib import Path

from maintenance_audit import SNAPSHOT_DRIFT_FAILURE_COMMITS

ROOT = Path(__file__).resolve().parent.parent
MEMORY_DIR = ROOT / ".claude/memory"
MEMORY_RELATIVE = ".claude/memory"
README = (MEMORY_DIR / "README.md").read_text()
CORRECTIONS = (MEMORY_DIR / "corrections.md").read_text()

WORKFLOW_DIR = ROOT / ".github/workflows"
CI_WORKFLOW = (WORKFLOW_DIR / "ci.yml").read_text()
AUDIT_WORKFLOW = (WORKFLOW_DIR / "maintenance-audit.yml").read_text()
THRESHOLDS = json.loads((ROOT / ".coverage-thresholds.json").read_text())

REPO_URL = "https://github.com/Danathar/atomic-image-builder"

# The fields every entry is written in, in this order. The README asks for an
# entry that records a belief, what was actually true, what the repo settled
# on and the trace it left; these are the four labels corrections.md uses for
# them.
FIELDS = ("Believed", "Actually", "Settled on", "Record")

# Each entry's heading, and the test below that joins what it settled on to
# the file that decides it. test_every_entry_is_joined_to_what_it_settled_on
# keeps this complete in both directions, so a new entry cannot be added
# without a join and a removed one cannot leave its join behind.
SETTLED_ON = {
    "A low coverage number is not always a gap": "test_maintenance_audit_tier_stays_advisory_and_its_error_paths_use_a_real_socket",
    "A red weekly job is worse than a quiet one": "test_snapshot_drift_fails_only_past_ordinary_upstream_movement",
    "Uncovered lines are sometimes a measurement artifact": "test_contrib_aib_writes_every_array_on_one_line",
    "A pinned dependency is not pinned if only some of it is": "test_every_pip_install_is_pinned_and_a_test_reads_the_workflow_pin",
    "Checking one mention is not checking consistency": "test_contributing_still_names_the_threshold_as_often_as_the_entry_says",
    "A path-scoped job must be triggered by its own tests": "test_the_container_job_is_triggered_by_the_suites_that_prove_the_image",
}

# What each record phrase says the link after it is. "answered by" and
# "fixed by" introduce the change; "review feedback on" names the pull request
# the review was left on. A link with no phrase before it is the issue.
LINK_KINDS = {
    "answered by": "pull",
    "fixed by": "pull",
    "review feedback on": "pull",
}

NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7}

LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def tracked(prefix: str) -> list[str]:
    """Tracked paths under a prefix. The working tree is the wrong thing to ask:
    a scratch note under .claude/memory/ is not something the repo ships."""
    result = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "--", prefix],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.splitlines()


def flat(text: str) -> str:
    """Markdown wraps, so a phrase can straddle a line break."""
    return re.sub(r"\s+", " ", text)


def entries() -> dict[str, str]:
    """corrections.md's `##` entries, heading to body."""
    parts = re.split(r"^## (.+)$", CORRECTIONS, flags=re.MULTILINE)
    return {heading.strip(): body for heading, body in zip(parts[1::2], parts[2::2])}


def entry(heading: str) -> str:
    return flat(entries()[heading])


def field(heading: str, name: str) -> str:
    """One labelled field of an entry, up to the next label."""
    body = entry(heading)
    labels = "|".join(re.escape(label) for label in FIELDS)
    match = re.search(rf"\*\*{re.escape(name)}:\*\* (.*?)(?= \*\*(?:{labels}):\*\*|$)", body)
    if match is None:
        raise AssertionError(f"corrections.md entry {heading!r} has no **{name}:** field")
    return match.group(1)


def number_word(text: str, pattern: str) -> int:
    """A count the prose spells in words or digits, found by a pattern with one group."""
    match = re.search(pattern, text)
    if match is None:
        raise AssertionError(f"no match for {pattern!r} in {text!r}")
    value = match.group(1).lower()
    return int(value) if value.isdigit() else NUMBER_WORDS[value]


def run_bodies(workflow: str) -> list[str]:
    """Every line of a workflow that a shell runs, with `#` comments dropped.

    A comment that says `pip install coverage` is not an install; reading it
    as one would let a pinned comment hide an unpinned command.
    """
    lines = []
    for line in workflow.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        lines.append(re.sub(r"\s+#.*$", "", stripped))
    return lines


class MemoryReadmeTests(unittest.TestCase):
    def test_the_file_table_is_exactly_the_tracked_files(self) -> None:
        # README.md describes the directory; every other tracked file in it
        # has to be in its table, and every row has to be a tracked file.
        listed = set(re.findall(r"^\| \[`([^`]+)`\]\(\1\) +\|", README, re.MULTILINE))
        self.assertTrue(listed, "the README's file table was not found")
        present = {
            path.removeprefix(f"{MEMORY_RELATIVE}/")
            for path in tracked(MEMORY_RELATIVE)
            if path != f"{MEMORY_RELATIVE}/README.md"
        }
        self.assertEqual(listed, present)

    def test_every_link_resolves_to_a_tracked_file(self) -> None:
        links = [target for _, target in LINK.findall(README)]
        self.assertGreaterEqual(len(links), 3)
        committed = set(tracked("."))
        for target in links:
            resolved = (MEMORY_DIR / target.split("#")[0]).resolve().relative_to(ROOT).as_posix()
            self.assertIn(resolved, committed, f"{MEMORY_RELATIVE}/README.md links to {target}")

    def test_the_companion_and_the_canonical_brief_are_the_ones_it_names(self) -> None:
        text = flat(README)
        self.assertIn("Its companion is [`../checkpoint.md`](../checkpoint.md)", text)
        self.assertIn("[`.github/copilot-instructions.md`](../../.github/copilot-instructions.md)", text)
        # corrections.md defers to the same brief for the current rules.
        self.assertIn("[`.github/copilot-instructions.md`](../../.github/copilot-instructions.md)", flat(CORRECTIONS))

    def test_gitignore_excludes_the_rest_of_claude_and_re_includes_this_directory(self) -> None:
        # "`.gitignore` excludes the rest of `.claude/` as personal config and
        # re-includes this directory." The order is the claim: a negation
        # that comes before the exclusion is overridden by it.
        self.assertIn(".gitignore` excludes the rest of `.claude/`", flat(README))
        rules = [line.strip() for line in (ROOT / ".gitignore").read_text().splitlines()]
        self.assertIn(".claude/*", rules)
        self.assertIn("!.claude/memory/", rules)
        self.assertLess(rules.index(".claude/*"), rules.index("!.claude/memory/"))
        ignored = subprocess.run(
            ["git", "-C", str(ROOT), "check-ignore", "-q", "--no-index", f"{MEMORY_RELATIVE}/corrections.md"],
            check=False,
        )
        self.assertEqual(ignored.returncode, 1, "git would ignore a new file under .claude/memory/")


class CorrectionEntryTests(unittest.TestCase):
    def test_every_entry_is_joined_to_what_it_settled_on(self) -> None:
        self.assertEqual(set(entries()), set(SETTLED_ON))
        for heading, test_name in SETTLED_ON.items():
            self.assertTrue(
                hasattr(SettledOnTests, test_name),
                f"{heading!r} names {test_name}, which SettledOnTests does not define",
            )

    def test_every_entry_carries_the_four_fields_in_order(self) -> None:
        for heading in entries():
            body = entry(heading)
            positions = [body.find(f"**{name}:**") for name in FIELDS]
            self.assertNotIn(-1, positions, f"{heading!r} is missing a field: {positions}")
            self.assertEqual(positions, sorted(positions), f"{heading!r} has its fields out of order")
            self.assertEqual(
                [body.count(f"**{name}:**") for name in FIELDS], [1] * len(FIELDS), heading
            )

    def test_every_record_links_to_this_repository_by_the_number_it_shows(self) -> None:
        # "Every entry cites the issue or pull request that records it, so an
        # entry can be checked rather than trusted."
        seen = 0
        for heading in entries():
            record = field(heading, "Record")
            links = LINK.findall(record)
            self.assertTrue(links, f"{heading!r} cites nothing")
            for text, url in links:
                seen += 1
                match = re.fullmatch(rf"{re.escape(REPO_URL)}/(issues|pull)/(\d+)", url)
                self.assertIsNotNone(match, f"{heading!r} links outside this repository: {url}")
                self.assertEqual(text, f"#{match.group(2)}", f"{heading!r} shows {text} for {url}")
        self.assertGreaterEqual(seen, len(SETTLED_ON))

    def test_every_record_link_is_the_kind_its_phrase_says(self) -> None:
        for heading in entries():
            record = field(heading, "Record")
            for match in LINK.finditer(record):
                before = record[: match.start()].rstrip().lower()
                phrase = next((p for p in LINK_KINDS if before.endswith(p)), None)
                expected = LINK_KINDS[phrase] if phrase else "issues"
                self.assertIn(f"/{expected}/", match.group(2), f"{heading!r}: {record}")
                if phrase is None:
                    self.assertEqual(before, "", f"{heading!r} has an unclassified phrase before {match.group(1)}")


class SettledOnTests(unittest.TestCase):
    def test_maintenance_audit_tier_stays_advisory_and_its_error_paths_use_a_real_socket(self) -> None:
        heading = "A low coverage number is not always a gap"
        self.assertIn("maintenance-audit tier", field(heading, "Believed"))
        self.assertIn("real loopback socket", field(heading, "Settled on"))
        # The tier is read as a map, not raised: it must stay advisory.
        self.assertIn("maintenance-audit", THRESHOLDS["advisory"])
        self.assertNotIn("maintenance-audit", THRESHOLDS["gated"])
        # And the unit tier proves those paths against a loopback server.
        helper = (ROOT / "tests/_local_http_server.py").read_text()
        self.assertIn('HTTPServer(("127.0.0.1", 0)', helper)
        audit_tests = (ROOT / "tests/test_maintenance_audit.py").read_text()
        self.assertRegex(audit_tests, r"(?m)^from _local_http_server import .*\blocal_http_server\b")

    def test_snapshot_drift_fails_only_past_ordinary_upstream_movement(self) -> None:
        heading = "A red weekly job is worse than a quiet one"
        actually = field(heading, "Actually")
        largest = number_word(actually, r"none by more than (\w+) commits")
        failed = number_word(actually, r"failed on (\w+) of its last")
        runs = number_word(actually, r"of its last (\w+)")
        # A threshold at or below the largest ordinary drift would put the
        # job back where #129 found it.
        self.assertGreater(SNAPSHOT_DRIFT_FAILURE_COMMITS, largest)
        # "red every Monday": the job the entry describes is the weekly one.
        self.assertIn("red every Monday", actually)
        self.assertRegex(AUDIT_WORKFLOW, r"cron: '\d+ \d+ \* \* 1'")
        # The workflow tells the same history; the two copies must agree.
        audit = flat(AUDIT_WORKFLOW.replace("#", " "))
        self.assertIn(f"red on {failed} of its last {runs} scheduled runs", audit)
        self.assertIn(f"none by more than {largest} commits", audit)
        self.assertIn("SNAPSHOT_DRIFT_FAILURE_COMMITS", audit)

    def test_contrib_aib_writes_every_array_on_one_line(self) -> None:
        heading = "Uncovered lines are sometimes a measurement artifact"
        self.assertIn("`contrib/aib`", field(heading, "Believed"))
        self.assertIn("one per line", field(heading, "Settled on"))
        script = (ROOT / "contrib/aib").read_text().splitlines()
        arrays = [line for line in script if re.search(r"\b\w+\+?=\(", line)]
        self.assertGreaterEqual(len(arrays), 5, "no array assignments found in contrib/aib")
        # An array literal that opens on one line and closes on another is
        # exactly what Bashcov under-reports.
        unclosed = [line for line in arrays if not re.search(r"\b\w+\+?=\([^()]*(\([^()]*\)[^()]*)*\)", line)]
        self.assertEqual(unclosed, [])

    def test_every_pip_install_is_pinned_and_a_test_reads_the_workflow_pin(self) -> None:
        heading = "A pinned dependency is not pinned if only some of it is"
        self.assertIn("exact versions in the workflow", field(heading, "Settled on"))
        installs = []
        for workflow in sorted(WORKFLOW_DIR.glob("*.yml")):
            for line in run_bodies(workflow.read_text()):
                match = re.search(r"\bpip3? install ([^;&|]+)", line)
                if match:
                    installs.append((workflow.name, match.group(1).split()))
        contributing = re.findall(r"^pip install (.+)$", (ROOT / "CONTRIBUTING.md").read_text(), re.MULTILINE)
        installs += [("CONTRIBUTING.md", args.split()) for args in contributing]
        self.assertGreaterEqual(len(installs), 6)
        for where, args in installs:
            packages = [arg for arg in args if not arg.startswith("-")]
            self.assertTrue(packages, f"{where}: pip install with no package")
            for package in packages:
                self.assertRegex(package, r"^[A-Za-z0-9_.-]+==[0-9][\w.]*$", f"{where} installs {package} unpinned")
        # "a test reading the workflow's own pin so the doc cannot go stale".
        self.assertIn("a test reading the workflow's own pin", field(heading, "Settled on"))
        readers = [
            path.name
            for path in (ROOT / "tests").glob("test_*.py")
            if 'read_text()' in (text := path.read_text())
            and '".github/workflows/ci.yml"' in text
            and re.search(r'r"pip install coverage==\\S\+', text)
        ]
        self.assertTrue(readers, "no test reads ci.yml's own pip pin any more")

    def test_contributing_still_names_the_threshold_as_often_as_the_entry_says(self) -> None:
        heading = "Checking one mention is not checking consistency"
        claimed = number_word(field(heading, "Actually"), r"CONTRIBUTING\.md names it (\w+) times")
        threshold = THRESHOLDS["gated"]["unit"]
        contributing = (ROOT / "CONTRIBUTING.md").read_text().splitlines()
        naming = [line for line in contributing if re.search(rf"\b{threshold}\b", line)]
        self.assertEqual(len(naming), claimed, naming)

    def test_the_container_job_is_triggered_by_the_suites_that_prove_the_image(self) -> None:
        heading = "A path-scoped job must be triggered by its own tests"
        settled = field(heading, "Settled on")
        self.assertIn("`tests/e2e/` is a trigger too", settled)
        match = re.search(r'git diff --quiet "\$base" "\$\{\{ github\.sha \}\}" -- ([^;]+);', CI_WORKFLOW)
        self.assertIsNotNone(match, "ci.yml's container-change pathspec has changed shape")
        pathspec = match.group(1).split()
        self.assertIn("tests/e2e/", pathspec)
        # "It is the one entry in that list not present in the image." Every
        # other entry is, or contains, something the Containerfile COPYs --
        # except the Containerfile itself, which is the recipe rather than an
        # ingredient.
        self.assertIn("the one entry in that list not present in the image", settled)
        copied = re.findall(r"^COPY (\S+) ", (ROOT / "Containerfile").read_text(), re.MULTILINE)
        self.assertGreaterEqual(len(copied), 3)
        absent = [
            entry_path
            for entry_path in pathspec
            if entry_path != "Containerfile" and not any(source.startswith(entry_path) for source in copied)
        ]
        self.assertEqual(absent, ["tests/e2e/"])


if __name__ == "__main__":
    unittest.main()

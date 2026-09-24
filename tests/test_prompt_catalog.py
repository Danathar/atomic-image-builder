"""Join `.github/prompts/` to the procedures, sections and commands it names.

Script: tests/test_prompt_catalog.py
What: Reads `.github/prompts/README.md` and the three `*.prompt.md` files and
      checks every pointer they hand-copy -- catalog rows, front matter, links,
      the `maintainer_docs/MAINTAINER.md` section titles each prompt sends its
      reader to, the flag it tells them to run, the workflow it triages, the
      constants it says to edit -- against the file that actually decides it.
Doing: Parses the catalog table into (label, target) rows and joins it both
       ways against the files on disk; parses each prompt's YAML front matter;
       resolves every relative link in the directory against the committed
       tree; splits MAINTAINER.md on its level-2 headings and asserts each
       named section exists *and* still contains the thing the prompt says it
       explains; runs `maintenance_audit.py --help` for the flag; reads
       `publish-image.yml`'s triggers, `atomic_image_builder.py`'s pin tables
       and `CONTRIBUTING.md`'s coverage-percentage guidance.
Why: `.github/prompts/` was opened by no test at any tier. `.coveragerc`
     measures Python modules only, ruff does not read Markdown, and the
     Containerfile copies `atomic_image_builder.py`, `template_snapshots/` and
     `container/entrypoint.sh` into the image and nothing else -- so the
     end-to-end tier cannot reach this directory either. No percentage moves
     when a pointer in here stops being true. These four files are what an
     agent reads before cutting a release, refreshing a pin or triaging the
     weekly audit, and a stale pointer does not error: it sends the reader to
     a section that no longer exists, or hands them a flag the script stopped
     accepting, in the middle of the one procedure with a wrong way to do it.
Goal: Make a renamed MAINTAINER.md section, a dropped catalog row, a removed
      `--skip-upstream`, a renamed pin table or a removed publish trigger fail
      here, instead of quietly turning the maintenance prompts into fiction.

The section joins are two-way on purpose. Asserting only that MAINTAINER.md
still has a *Before pushing* heading leaves the prompt free to point somewhere
else; asserting only that the prompt says *Before pushing* leaves the heading
free to be renamed. Both directions have to hold for the pointer to work, so
both are asserted, and the section's contents are checked for the thing the
prompt claims is in it -- a heading that kept its name after its body moved
elsewhere is the same broken pointer with a passing test.

The Monday cron behind the catalog's "The Monday audit ran" is deliberately
not re-asserted here: `tests/test_metrics_doc.py` already pins
`maintenance-audit.yml`'s schedule to a Monday for docs/metrics.md, and a
second copy of that assertion would move in lockstep with the first without
ever catching anything it missed.
"""

from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PROMPT_DIR = ROOT / ".github/prompts"
CATALOG_PATH = PROMPT_DIR / "README.md"

MAINTAINER_PATH = ROOT / "maintainer_docs/MAINTAINER.md"
CONTRIBUTING_PATH = ROOT / "CONTRIBUTING.md"
COPILOT_PATH = ROOT / ".github/copilot-instructions.md"
AUDIT_SCRIPT = ROOT / "maintenance_audit.py"
AUDIT_WORKFLOW = ROOT / ".github/workflows/maintenance-audit.yml"
PUBLISH_WORKFLOW = ROOT / ".github/workflows/publish-image.yml"
BUILDER_PATH = ROOT / "atomic_image_builder.py"

# The prompts, named rather than discovered. Discovery would make every
# per-prompt assertion below vacuous the moment a file stopped being shipped,
# which is precisely the case they exist to catch; the glob-vs-this-list check
# in `test_shipped_prompts_are_the_ones_this_module_asserts_against` is what
# stops a new prompt being added without joining it here.
CUT_RELEASE = "cut-release.prompt.md"
REFRESH_PINS = "refresh-action-pins.prompt.md"
TRIAGE_AUDIT = "triage-weekly-audit.prompt.md"

PROMPT_FILES = (CUT_RELEASE, REFRESH_PINS, TRIAGE_AUDIT)

# Each prompt refuses to restate its procedure and sends the reader to a named
# MAINTAINER.md section instead. The title is the whole pointer: it is written
# `*Like This*` in the prompt and `## Like This` in the guide, and nothing but
# this test connects the two spellings.
NAMED_SECTIONS = {
    CUT_RELEASE: ("Cutting a release", "Before pushing"),
    REFRESH_PINS: ("Refreshing action pins",),
    TRIAGE_AUDIT: ("Reading the weekly audit",),
}

MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
CATALOG_ROW = re.compile(r"^\|\s*(?P<label>.+?)\s*\|\s*(?P<when>.*?)\s*\|\s*$")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def level_two_sections(text: str) -> dict[str, str]:
    """Map each `## ` heading to its body, which runs to the next `## `.

    Subsection headings stay inside the body they sit under: the guide keeps
    the graduated-advisory detail under *Reading the weekly audit* as `###`
    blocks, and a prompt that sends its reader to the level-2 title is sending
    them to all of it.
    """
    sections: dict[str, str] = {}
    current: str | None = None
    body: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if current is not None:
                sections[current] = "\n".join(body)
            current = line[3:].strip()
            body = []
        elif current is not None:
            body.append(line)
    if current is not None:
        sections[current] = "\n".join(body)
    return sections


def relative_links(text: str) -> list[tuple[str, str]]:
    """The (label, target) pairs of every link that names a file in the tree."""
    found = []
    for label, target in MARKDOWN_LINK.findall(text):
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        found.append((label, target))
    return found


class PromptCatalogTableTests(unittest.TestCase):
    """The catalog table is the index. A row that drifts hides a prompt."""

    def setUp(self) -> None:
        self.catalog = read(CATALOG_PATH)
        self.rows = self._body_rows()

    def _body_rows(self) -> list[tuple[str, str]]:
        rows: list[tuple[str, str]] = []
        seen_delimiter = False
        for line in self.catalog.splitlines():
            if not line.startswith("|"):
                continue
            match = CATALOG_ROW.match(line)
            self.assertIsNotNone(match, f"unreadable catalog row {line!r}")
            assert match is not None  # narrows for type checkers; assertIsNotNone is the test
            label, when = match.group("label"), match.group("when")
            if set(label) <= set("- :") and set(when) <= set("- :"):
                seen_delimiter = True
                continue
            if not seen_delimiter:
                continue  # the header row
            rows.append((label, when))
        self.assertTrue(seen_delimiter, "catalog table has no delimiter row")
        return rows

    def test_catalog_lists_every_shipped_prompt(self) -> None:
        """Both directions: no prompt missing a row, no row without a file."""
        shipped = sorted(p.name for p in PROMPT_DIR.glob("*.prompt.md"))
        linked = sorted(
            target
            for label, _ in self.rows
            for _, target in relative_links(label)[:1]
        )
        self.assertEqual(
            linked, shipped,
            "the catalog table and .github/prompts/*.prompt.md disagree; "
            "a prompt with no row is one nobody finds, and a row with no file "
            "is a broken link",
        )

    def test_shipped_prompts_are_the_ones_this_module_asserts_against(self) -> None:
        """A new prompt has to be joined here, not just listed in the table."""
        self.assertEqual(
            sorted(p.name for p in PROMPT_DIR.glob("*.prompt.md")),
            sorted(PROMPT_FILES),
            "a prompt was added or removed without updating PROMPT_FILES, so "
            "its front matter, links and MAINTAINER.md pointers are unasserted",
        )

    def test_each_row_label_names_its_own_file(self) -> None:
        """`[`cut-release`](cut-release.prompt.md)` -- label and target agree."""
        for label, _ in self.rows:
            links = relative_links(label)
            self.assertEqual(len(links), 1, f"catalog row label {label!r} is not one link")
            text, target = links[0]
            self.assertEqual(
                text.strip("`"), target[: -len(".prompt.md")],
                f"catalog row labelled {text!r} links to {target!r}",
            )

    def test_every_row_says_when_to_reach_for_it(self) -> None:
        """The second column is the whole reason the table beats an `ls`."""
        for label, when in self.rows:
            self.assertTrue(when.strip(), f"catalog row {label!r} has an empty When cell")

    def test_directory_holds_only_the_catalog_and_prompts(self) -> None:
        """An unlisted file here is work the catalog says does not exist."""
        listed = {"README.md", *PROMPT_FILES}
        present = {p.name for p in PROMPT_DIR.iterdir() if p.is_file()}
        self.assertEqual(
            present, listed,
            "a file under .github/prompts/ is neither the catalog nor a "
            "catalogued prompt; the catalog's 'One-off work does not belong "
            "here' is the rule it breaks",
        )


class PromptFrontMatterTests(unittest.TestCase):
    """`mode: agent` is what makes these files prompts rather than notes."""

    def front_matter(self, name: str) -> dict[str, str]:
        lines = read(PROMPT_DIR / name).splitlines()
        self.assertEqual(lines[0], "---", f"{name} does not open with front matter")
        end = lines.index("---", 1)
        parsed: dict[str, str] = {}
        for line in lines[1:end]:
            key, separator, value = line.partition(":")
            self.assertTrue(separator, f"unreadable front-matter line {line!r} in {name}")
            parsed[key.strip()] = value.strip()
        return parsed

    def test_every_prompt_declares_agent_mode(self) -> None:
        for name in PROMPT_FILES:
            self.assertEqual(
                self.front_matter(name).get("mode"), "agent",
                f"{name} is not declared `mode: agent`, so it is not offered as "
                "an agent prompt however good its contents are",
            )

    def test_every_prompt_carries_a_distinct_description(self) -> None:
        """The description is the only text shown when picking a prompt."""
        descriptions: dict[str, str] = {}
        for name in PROMPT_FILES:
            description = self.front_matter(name).get("description", "")
            self.assertTrue(description, f"{name} has no front-matter description")
            self.assertNotIn(
                description, descriptions,
                f"{name} and {descriptions.get(description)} share a description",
            )
            descriptions[description] = name


class PromptLinkTests(unittest.TestCase):
    """Every link in the directory, resolved against the committed tree."""

    def test_every_relative_link_resolves(self) -> None:
        for path in sorted(PROMPT_DIR.glob("*.md")):
            for label, target in relative_links(read(path)):
                resolved = (path.parent / target.split("#", 1)[0]).resolve()
                self.assertTrue(
                    resolved.exists(),
                    f"{path.name} links {label!r} to {target!r}, which does not exist",
                )

    def test_every_prompt_opens_by_pointing_at_the_canonical_brief(self) -> None:
        """The catalog promises all three do this; nothing else checks it."""
        self.assertTrue(COPILOT_PATH.exists(), "the canonical brief is missing")
        for name in PROMPT_FILES:
            body = read(PROMPT_DIR / name)
            self.assertIn(
                "(../copilot-instructions.md)", body,
                f"{name} does not link .github/copilot-instructions.md, which "
                "the catalog tells the reader every prompt here does",
            )

    def test_prompts_name_the_guide_by_its_real_path(self) -> None:
        for name in PROMPT_FILES:
            self.assertIn(
                "`maintainer_docs/MAINTAINER.md`", read(PROMPT_DIR / name),
                f"{name} stopped naming maintainer_docs/MAINTAINER.md, the "
                "authoritative procedure it is supposed to defer to",
            )
        self.assertTrue(MAINTAINER_PATH.exists())


class NamedSectionTests(unittest.TestCase):
    """Two-way: the prompt still names the section, and it still exists."""

    def setUp(self) -> None:
        self.sections = level_two_sections(read(MAINTAINER_PATH))

    def test_named_sections_exist_and_are_still_named(self) -> None:
        for name, titles in NAMED_SECTIONS.items():
            body = read(PROMPT_DIR / name)
            for title in titles:
                self.assertIn(
                    f"*{title}*", body,
                    f"{name} no longer points at MAINTAINER.md's *{title}*",
                )
                self.assertIn(
                    title, self.sections,
                    f"{name} points at MAINTAINER.md's *{title}*, which is not "
                    f"a heading there; the guide has {sorted(self.sections)}",
                )

    def test_cutting_a_release_still_holds_the_table_of_consumers(self) -> None:
        """cut-release: 'MAINTAINER.md has the table of what reads it'."""
        body = self.sections["Cutting a release"]
        self.assertIn("VERSION", body)
        for consumer in ("publish-image.yml", "homebrew_formula.py"):
            self.assertIn(
                consumer, body,
                f"MAINTAINER.md's *Cutting a release* no longer names {consumer} "
                "among what derives from the version constant",
            )

    def test_before_pushing_still_explains_the_brew_audit_trap(self) -> None:
        """cut-release sends the reader here for exactly this warning."""
        body = self.sections["Before pushing"]
        self.assertIn("brew audit", body)
        self.assertIn(
            "ruby -c", body,
            "*Before pushing* warns off `brew audit` but no longer says what to "
            "run instead, which is the half cut-release.prompt.md relies on",
        )

    def test_refreshing_action_pins_still_gives_the_compare_invocation(self) -> None:
        """refresh-action-pins: 'MAINTAINER.md gives the exact `gh api ...`'."""
        body = self.sections["Refreshing action pins"]
        self.assertIn("gh api", body)
        self.assertIn(
            "/compare/", body,
            "*Refreshing action pins* no longer carries the compare call that "
            "establishes the direction of travel, which is step 1 of the prompt",
        )

    def test_reading_the_weekly_audit_still_defines_both_categories(self) -> None:
        """triage-weekly-audit: that section 'defines the difference'."""
        body = self.sections["Reading the weekly audit"]
        for word in ("Failures", "Advisories"):
            self.assertIn(
                word, body,
                f"*Reading the weekly audit* no longer defines {word.lower()}; "
                "the triage prompt's whole first decision rests on the split",
            )


class NamedMachineryTests(unittest.TestCase):
    """The commands, flags, files and constants the prompts hand the reader."""

    def test_maintenance_audit_still_accepts_skip_upstream(self) -> None:
        """refresh-action-pins step 4 runs it verbatim."""
        completed = subprocess.run(
            [sys.executable, str(AUDIT_SCRIPT), "--help"],
            capture_output=True, text=True, cwd=ROOT, check=True,
        )
        self.assertIn(
            "--skip-upstream", completed.stdout,
            "maintenance_audit.py no longer accepts --skip-upstream, which "
            "refresh-action-pins.prompt.md tells the reader to run",
        )

    def test_the_workflow_the_triage_prompt_triages_exists(self) -> None:
        self.assertIn("maintenance-audit.yml", read(PROMPT_DIR / TRIAGE_AUDIT))
        self.assertTrue(
            AUDIT_WORKFLOW.exists(),
            "triage-weekly-audit.prompt.md triages maintenance-audit.yml, "
            "which is not in .github/workflows/",
        )

    def test_a_release_still_produces_two_image_publishes(self) -> None:
        """cut-release's 'Do not be surprised by two image publishes'.

        The prompt tells the reader to expect one publish from the release
        event and one from merging the formula pull request into `main`. That is only
        true while publish-image.yml carries both triggers; drop either and
        the advice becomes a reason to go looking for a bug that is not there.
        """
        workflow = read(PUBLISH_WORKFLOW)
        self.assertRegex(workflow, r"(?m)^\s*push:\s*$")
        self.assertRegex(workflow, r"(?m)^\s*branches:\s*\[main\]\s*$")
        self.assertRegex(workflow, r"(?m)^\s*release:\s*$")
        self.assertRegex(workflow, r"(?m)^\s*types:\s*\[published\]\s*$")

    def test_the_bump_is_still_one_constant(self) -> None:
        """cut-release step 2: 'Bump the one constant'."""
        assignments = re.findall(r"(?m)^VERSION\s*=", read(BUILDER_PATH))
        self.assertEqual(
            len(assignments), 1,
            f"atomic_image_builder.py has {len(assignments)} module-level VERSION "
            "assignments; cut-release.prompt.md promises exactly one line to edit",
        )

    def test_both_pin_locations_the_refresh_prompt_names_still_exist(self) -> None:
        """'Update the SHA in both places' -- neither place is discovered."""
        builder = read(BUILDER_PATH)
        for table in ("ACTION_PINS", "ACTION_REF_PINS"):
            self.assertRegex(
                builder, rf"(?m)^{table}\b",
                f"atomic_image_builder.py no longer defines {table}, one of the "
                "two pin locations refresh-action-pins.prompt.md sends the "
                "reader to",
            )
        snapshot_workflows = sorted(ROOT.glob("template_snapshots/*/.github/workflows/*.yml"))
        self.assertTrue(
            snapshot_workflows,
            "no workflow file under template_snapshots/, so the prompt's second "
            "pin location does not exist",
        )

    def test_the_rewrite_helper_the_prompt_relies_on_still_exists(self) -> None:
        """Step 3's ref-pin entry only does anything through this function."""
        self.assertRegex(read(BUILDER_PATH), r"(?m)^def pin_action_uses_line\(")

    def test_generated_repos_still_ship_dependabot(self) -> None:
        """refresh-action-pins' 'so this matters more than it looks'."""
        snapshots = sorted(p for p in (ROOT / "template_snapshots").iterdir() if p.is_dir())
        self.assertTrue(snapshots, "no template snapshots to render a repo from")
        for snapshot in snapshots:
            self.assertTrue(
                (snapshot / ".github/dependabot.yml").exists(),
                f"{snapshot.name} ships no .github/dependabot.yml, so the "
                "prompt's reason a stale pin is urgent no longer holds",
            )

    def test_contributing_still_carries_the_coverage_percentage_guidance(self) -> None:
        """triage-weekly-audit: 'CONTRIBUTING.md explains why at length'."""
        contributing = read(CONTRIBUTING_PATH)
        self.assertIn(
            "Do not file the percentage itself as a finding", contributing,
            "CONTRIBUTING.md no longer states the rule triage-weekly-audit.prompt.md "
            "forwards its reader to",
        )
        self.assertIn(
            "issues/123", contributing,
            "CONTRIBUTING.md no longer links issue #123, the worked example the "
            "triage prompt cites by number",
        )


if __name__ == "__main__":
    unittest.main()

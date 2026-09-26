"""Join ROADMAP.md, and the README facts it restates, to the files that decide them.

Script: tests/test_roadmap_doc.py
What: Reads ROADMAP.md as a subject and checks each claim it makes about the
      project today -- the release series and the beta warning it quotes,
      the supported bases, the two build methods and the template each one
      starts from, the README gaps it says it "pulled directly", the state
      file that marks a repo as managed, and the "five-stage runtime" --
      against VERSION, BASE_IMAGES, METHOD_DISPLAY, the template constants,
      STATE_FILE, ARCHITECTURE.md and the README.
      It also reads the README's own "Supported bases" line and build-method
      bullet, which ROADMAP.md copies and which no test joined to the code.
Doing: Parses the base list out of each document's prose and compares it to
       BASE_IMAGES as a set, both ways. Reads number words out of the prose
       and compares them to counts taken from the source. Maps each ROADMAP
       gap to the README bullet it came from through a closed table, checked
       in both directions, with the one README bullet that is not a gap
       exempted by name. Classifies every backticked token and every `##`
       section exhaustively, so a new one has to be joined before it passes.
Why: ROADMAP.md (added in #458) is a summary of other documents, and a summary
     is the copy that nobody updates: no test opened it, so a version bump, a
     new base image, a third build method or a README gap that is closed
     would leave it describing a project that no longer exists. The README
     line it copies the bases from was not joined to BASE_IMAGES either.
     `.coveragerc` measures Python modules and ruff does not read Markdown,
     so no percentage moves when either drifts.
Goal: Make a version, base, method, template, gap, state file or stage count
      that changes without ROADMAP.md (or the README line it copies) fail here.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import atomic_image_builder as aib  # noqa: E402

DOC = (ROOT / "ROADMAP.md").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")
ARCHITECTURE = (ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")

NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7}

# ROADMAP.md's `##` sections, in order. A new section is a new set of claims,
# so it has to be added here, and joined below, before this file passes.
SECTIONS = (
    "Current state",
    "Gaps the README already states",
    "Near-term priorities",
    "Longer-term / open questions",
)

README_GAP_SECTION = "What it does not do"

# Each gap ROADMAP.md lists, by its bold lead, mapped to the start of the
# README bullet it was "pulled directly" from. Closed in both directions.
GAP_SOURCES = {
    "Advanced BlueBuild modules beyond the guided wizard are out of scope.": (
        "Advanced BlueBuild modules beyond the guided wizard are out of scope, by design."
    ),
    "Repos not created by the tool are never adopted.": "Does not adopt repos it did not create",
}

# README bullets under "What it does not do" that are a property of the tool,
# not a gap anyone would pick up next, so ROADMAP.md rightly leaves them out.
NOT_A_GAP = ("**Leaves the system you run it on alone**",)

# The only things ROADMAP.md may put in backticks: the two template repos and
# the state file. Anything else is an unjoined claim.
BACKTICK_OWNERS = {
    aib.CONTAINERFILE_TEMPLATE_REPO: "CONTAINERFILE_TEMPLATE_REPO",
    aib.BLUEBUILD_TEMPLATE_REPO: "BLUEBUILD_TEMPLATE_REPO",
    aib.STATE_FILE: "STATE_FILE",
}

# Which template constant each method key starts from.
METHOD_TEMPLATES = {
    "containerfile": aib.CONTAINERFILE_TEMPLATE_REPO,
    "bluebuild": aib.BLUEBUILD_TEMPLATE_REPO,
}


def squash(text: str) -> str:
    """Markdown wraps; compare prose with every run of whitespace as one space."""
    return re.sub(r"\s+", " ", text).strip()


def section(text: str, title: str, level: str = "##") -> str:
    match = re.search(
        rf"^{level} {re.escape(title)}\n(.*?)(?=^#{{1,{len(level)}}} |\Z)",
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"no `{level} {title}` section")
    return match.group(1)


def bullets(body: str) -> list[str]:
    """Top-level `- ` bullets, each with its wrapped continuation lines joined."""
    items: list[str] = []
    open_item = False
    for line in body.splitlines():
        if line.startswith("- "):
            items.append(line[2:])
            open_item = True
        elif open_item and line.startswith("  ") and line.strip():
            items[-1] += " " + line.strip()
        else:
            open_item = False
    return [squash(item) for item in items]


def current_state_bullet(lead: str) -> str:
    found = [b for b in bullets(section(DOC, "Current state")) if b.startswith(lead)]
    if len(found) != 1:
        raise AssertionError(f"ROADMAP.md's Current state has {len(found)} bullets starting {lead!r}")
    return found[0]


def universal_blue_keys() -> set[str]:
    return {image.key for image in aib.BASE_IMAGES if image.provider == "Universal Blue"}


def universal_blue_families() -> set[str]:
    """Bazzite, Aurora, Bluefin: the first word of each Universal Blue name."""
    return {
        image.name.split()[0]
        for image in aib.BASE_IMAGES
        if image.provider == "Universal Blue"
    }


def fedora_short_names() -> set[str]:
    """"Fedora Sway Atomic" -> "Sway": how both documents spell a Fedora base."""
    names = set()
    for image in aib.BASE_IMAGES:
        if image.provider != "Fedora Atomic":
            continue
        short = image.name.removeprefix("Fedora ").removesuffix(" Atomic")
        names.add(short)
    return names


def fedora_list(text: str) -> set[str]:
    """The comma list after `Fedora Atomic` ... `:` (README) or `(` (ROADMAP)."""
    plain = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text).replace("**", "")
    match = re.search(r"Fedora Atomic[^:(]*[:(]\s*([^.)]+)", plain)
    if match is None:
        raise AssertionError("no Fedora Atomic base list")
    return {name.strip() for name in match.group(1).split(",")}


class Outline(unittest.TestCase):
    def test_the_sections_are_the_ones_this_file_joins(self) -> None:
        found = tuple(re.findall(r"^## (.+)$", DOC, flags=re.MULTILINE))
        self.assertEqual(found, SECTIONS)

    def test_every_backticked_token_has_a_source_in_the_code(self) -> None:
        tokens = re.findall(r"`([^`]+)`", DOC)
        self.assertTrue(tokens)
        self.assertEqual(set(tokens) - set(BACKTICK_OWNERS), set())

    def test_every_backtick_owner_is_still_used(self) -> None:
        # The table above cannot go dead: each entry is a token ROADMAP.md
        # still names, so dropping one from the document is caught here.
        self.assertEqual(set(re.findall(r"`([^`]+)`", DOC)), set(BACKTICK_OWNERS))


class CurrentState(unittest.TestCase):
    def test_the_series_is_the_one_version_names(self) -> None:
        series = ".".join(aib.VERSION.split(".")[:2])
        lead = re.search(r"\*\*(\d+\.\d+), beta\.\*\*", DOC)
        self.assertIsNotNone(lead, "ROADMAP.md no longer leads with `**X.Y, beta.**`")
        self.assertEqual(lead.group(1), series)

    def test_the_readme_quote_is_the_readmes_beta_warning(self) -> None:
        bullet = current_state_bullet("**")
        quote = re.search(r'says outright: "([^"]+)"', bullet)
        self.assertIsNotNone(quote, "the Current state bullet no longer quotes the README")
        warning = next(
            (line for line in README.splitlines() if line.startswith("> **") and " beta" in line),
            None,
        )
        self.assertIsNotNone(warning, "README.md has no `> **X.Y beta ...` warning line")
        self.assertIn(squash(quote.group(1)), squash(warning.replace("**", "")))

    def test_the_universal_blue_families_are_the_catalogs(self) -> None:
        bullet = current_state_bullet("Supported bases:")
        match = re.search(r"Universal Blue \(([^)]+)\)", bullet)
        self.assertIsNotNone(match)
        named = set(re.findall(r"[A-Z][a-z]+", match.group(1)))
        self.assertEqual(named, universal_blue_families())

    def test_the_fedora_variants_are_the_catalogs(self) -> None:
        bullet = current_state_bullet("Supported bases:")
        self.assertEqual(fedora_list(bullet), fedora_short_names())

    def test_the_providers_are_the_catalogs(self) -> None:
        bullet = current_state_bullet("Supported bases:")
        named = {p for p in ("Universal Blue", "Fedora Atomic") if p in bullet}
        self.assertEqual(named, {image.provider for image in aib.BASE_IMAGES})

    def test_the_method_count_is_the_number_of_methods(self) -> None:
        match = re.search(r"^- (\w+) build methods:", DOC, flags=re.MULTILINE)
        self.assertIsNotNone(match, "ROADMAP.md no longer counts the build methods")
        self.assertEqual(NUMBER_WORDS[match.group(1).lower()], len(aib.METHOD_DISPLAY))

    def test_each_method_names_the_template_it_starts_from(self) -> None:
        lead = re.search(r"^- (\w+ build methods:)", DOC, flags=re.MULTILINE)
        self.assertIsNotNone(lead, "ROADMAP.md no longer counts the build methods")
        bullet = current_state_bullet(lead.group(1))
        pairs = dict(re.findall(r"(\w+) \(from `([^`]+)`\)", bullet))
        expected = {aib.METHOD_DISPLAY[key]: repo for key, repo in METHOD_TEMPLATES.items()}
        self.assertEqual(pairs, expected)

    def test_the_method_template_table_covers_every_method(self) -> None:
        self.assertEqual(set(METHOD_TEMPLATES), set(aib.METHOD_DISPLAY))


class ReadmeSources(unittest.TestCase):
    """The README lines ROADMAP.md copies, which nothing joined to the code."""

    def supported_bases(self) -> str:
        found = [
            squash(p)
            for p in re.split(r"\n\s*\n", section(README, "What it does"))
            if p.startswith("Supported bases")
        ]
        self.assertEqual(len(found), 1, "README's What it does has no single `Supported bases` paragraph")
        return found[0]

    def test_the_universal_blue_bases_are_the_catalogs_both_ways(self) -> None:
        # "[Bazzite](...) (also GNOME, DX, DX GNOME)" names bazzite plus
        # bazzite-gnome, bazzite-dx and bazzite-dx-gnome.
        para = self.supported_bases()
        ub = para.split("Fedora Atomic", 1)[0]
        named: set[str] = set()
        for family, variants in re.findall(r"\[(\w+)\]\([^)]+\)(?: \(also ([^)]+)\))?", ub):
            if family == "Universal":
                continue
            base = family.lower()
            named.add(base)
            for variant in filter(None, (v.strip() for v in variants.split(","))):
                named.add(f"{base}-{variant.lower().replace(' ', '-')}")
        self.assertEqual(named, universal_blue_keys())

    def test_the_fedora_bases_are_the_catalogs_both_ways(self) -> None:
        self.assertEqual(fedora_list(self.supported_bases()), fedora_short_names())

    def test_each_build_method_names_the_template_it_starts_from(self) -> None:
        bullet = next(
            b for b in bullets(section(README, "What it does")) if b.startswith("Choose the build method")
        )
        pairs = dict(
            re.findall(r"\*\*(\w+)\*\* \(from (?:a bundled snapshot of )?\[`([^`]+)`\]", bullet)
        )
        expected = {aib.METHOD_DISPLAY[key]: repo for key, repo in METHOD_TEMPLATES.items()}
        self.assertEqual(pairs, expected)

    def test_the_readme_names_the_state_file_the_tool_writes(self) -> None:
        gaps = section(README, README_GAP_SECTION, level="###")
        self.assertIn(f"a repo without `{aib.STATE_FILE}` is not treated as managed", squash(gaps))


class Gaps(unittest.TestCase):
    def roadmap_gaps(self) -> list[str]:
        leads = []
        for bullet in bullets(section(DOC, "Gaps the README already states")):
            match = re.match(r"\*\*(.+?)\*\*", bullet)
            self.assertIsNotNone(match, f"gap bullet without a bold lead: {bullet!r}")
            leads.append(match.group(1))
        return leads

    def readme_gaps(self) -> list[str]:
        return bullets(section(README, README_GAP_SECTION, level="###"))

    def test_the_section_it_cites_is_a_readme_heading(self) -> None:
        body = squash(section(DOC, "Gaps the README already states"))
        self.assertIn(f'pulled directly from the "{README_GAP_SECTION}" section', body)
        self.assertRegex(README, rf"(?m)^#+ {re.escape(README_GAP_SECTION)}$")

    def test_every_roadmap_gap_is_mapped(self) -> None:
        self.assertEqual(set(self.roadmap_gaps()), set(GAP_SOURCES))

    def test_every_mapped_gap_is_still_a_readme_bullet(self) -> None:
        readme = self.readme_gaps()
        for lead, source in GAP_SOURCES.items():
            with self.subTest(lead=lead):
                self.assertEqual(sum(b.startswith(source) for b in readme), 1)

    def test_every_readme_gap_is_in_the_roadmap_or_named_as_not_a_gap(self) -> None:
        for bullet in self.readme_gaps():
            with self.subTest(bullet=bullet):
                self.assertTrue(
                    any(bullet.startswith(s) for s in GAP_SOURCES.values())
                    or any(bullet.startswith(n) for n in NOT_A_GAP),
                    "a README gap ROADMAP.md does not list",
                )

    def test_the_not_a_gap_exemption_is_still_a_readme_bullet(self) -> None:
        for name in NOT_A_GAP:
            with self.subTest(name=name):
                self.assertTrue(any(b.startswith(name) for b in self.readme_gaps()))

    def test_the_adoption_gap_names_the_state_file_the_tool_writes(self) -> None:
        body = squash(section(DOC, "Gaps the README already states"))
        self.assertIn(f"A repo without `{aib.STATE_FILE}` stays untouched", body)


class Runtime(unittest.TestCase):
    def test_the_stage_count_is_architectures(self) -> None:
        stated = re.search(r"the (\w+)-stage runtime", squash(DOC))
        self.assertIsNotNone(stated, "ROADMAP.md no longer names the N-stage runtime")
        arch = re.search(r"serves one of (\w+) stages:\n\n((?:\d+\. .*\n(?: {3}.*\n)*)+)", ARCHITECTURE)
        self.assertIsNotNone(arch, "ARCHITECTURE.md no longer lists its stages")
        listed = len(re.findall(r"^\d+\. ", arch.group(2), flags=re.MULTILINE))
        self.assertEqual(NUMBER_WORDS[stated.group(1)], listed)


if __name__ == "__main__":
    unittest.main()

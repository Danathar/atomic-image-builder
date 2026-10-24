"""Hold every section of the documentation pages to a row in docs/section-map.txt.

Script: tests/test_docs_section_map.py
What: Reads every tracked documentation page's `##` and `###` headings and
      checks them against docs/section-map.txt, which gives each heading either
      `joined:` and the test that reads it, or `prose:` and a reason.
Doing: Lists the pages with `git ls-files`, reads headings outside fenced
       blocks, and for each `joined:` row parses the named test to see that it
       exists and opens the page somewhere other than a docstring or comment.
Why: Pages here keep stating facts about the automation -- what a workflow
     does, what the settings file denies, what the command gate refuses -- and
     going stale when the machine changes. Quality passes found that twelve
     times across two repositories (#805 lists them), each one a section no
     test read, each fixed by its own issue and join test. The common cause
     was that leaving a section unchecked was the silent default.
Goal: A new or renamed section has to be classified in the pull request that
      adds it, so an unchecked section of machine claims is a visible
      `prose: unchecked:` line in a reviewed diff rather than nothing at all.

This does not prove any sentence true, and a `joined:` row only means the test
opens the page: whether it reads that section is the reviewer's call, made
once, when the row is written.
"""

from __future__ import annotations

import ast
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAP = ROOT / "docs/section-map.txt"
MAP_RELATIVE = MAP.relative_to(ROOT).as_posix()
THIS_TEST = Path(__file__).resolve().relative_to(ROOT).as_posix()

# Root-level pages, docs/ without its dated subdirectories, maintainer_docs/,
# and the canonical agent brief. template_snapshots/ is vendored upstream text
# and tests/e2e/README.md describes a harness, not the automation.
TRACKED_PAGE = re.compile(r"^(?:[^/]+|docs/[^/]+|maintainer_docs/[^/]+)\.md$")
EXTRA_PAGES = {".github/copilot-instructions.md"}

HEADING = re.compile(r"^(#{2,3}) +(.+?)(?: +#+)? *$")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
PAGE_LINE = re.compile(r"^\[(?P<page>[^\]]+)\]$")
ROW_LINE = re.compile(r"^  (?P<kind>joined|prose): *(?P<value>.*)$")


def tracked_pages() -> set[str]:
    listing = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z", "--", "*.md"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    names = {name for name in listing.split("\0") if name}
    return {name for name in names if TRACKED_PAGE.match(name)} | (EXTRA_PAGES & names)


def headings(text: str) -> list[str]:
    """`##`/`###` headings outside fenced blocks, as `## Text`."""
    found = []
    fence = ""
    for line in text.splitlines():
        opened = FENCE.match(line)
        if opened:
            marker = opened.group(1)
            if not fence:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = ""
            continue
        if fence:
            continue
        match = HEADING.match(line)
        if match:
            found.append(f"{match.group(1)} {match.group(2)}")
    return found


def parse_map(text: str) -> dict[str, dict[str, tuple[str, str]]]:
    """Page -> heading -> (kind, value). Raises on any line it cannot place."""
    pages: dict[str, dict[str, tuple[str, str]]] = {}
    page = None
    heading = None
    for number, line in enumerate(text.splitlines(), start=1):
        where = f"{MAP_RELATIVE}:{number}"
        if not line.strip() or (line.startswith("#") and not HEADING.match(line)):
            continue
        if heading is not None and not ROW_LINE.match(line):
            raise AssertionError(f"{where}: {heading!r} has no joined:/prose: line under it")
        if match := PAGE_LINE.match(line):
            page = match.group("page")
            if page in pages:
                raise AssertionError(f"{where}: [{page}] is listed twice")
            pages[page] = {}
        elif HEADING.match(line):
            if page is None:
                raise AssertionError(f"{where}: heading before any [page] line")
            if line in pages[page]:
                raise AssertionError(f"{where}: {line!r} is listed twice under [{page}]")
            heading = line
        elif match := ROW_LINE.match(line):
            if heading is None:
                raise AssertionError(f"{where}: classification with no heading above it")
            value = match.group("value").strip()
            if not value:
                raise AssertionError(f"{where}: {match.group('kind')}: needs a value")
            pages[page][heading] = (match.group("kind"), value)
            heading = None
        else:
            raise AssertionError(f"{where}: neither a [page], a heading nor a classification: {line!r}")
    if heading is not None:
        raise AssertionError(f"{MAP_RELATIVE}: {heading!r} has no joined:/prose: line under it")
    return pages


def docstrings(tree: ast.AST) -> set[int]:
    """ids of the string nodes that are module, class or function docstrings."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                found.add(id(body[0].value))
    return found


def opens_page(test_text: str, suffix: str, page: str) -> bool:
    """Whether a test names the page in code, not just in a docstring or comment.

    Tests spell a path either whole (`ROOT / "docs/using.md"`) or in parts
    (`ROOT / "docs" / "using.md"`), so the file name is what is looked for.
    """
    name = Path(page).name
    if suffix == ".sh":
        return any(name in line for line in test_text.splitlines() if not line.lstrip().startswith("#"))
    tree = ast.parse(test_text)
    skipped = docstrings(tree)
    return any(
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in skipped
        and name in node.value
        for node in ast.walk(tree)
    )


class HeadingReaderTests(unittest.TestCase):
    def test_reads_both_levels_and_nothing_else(self) -> None:
        text = "# Title\n## One\ntext\n### Two\n#### Three\n##No space\n"
        self.assertEqual(headings(text), ["## One", "### Two"])

    def test_skips_headings_inside_a_fence(self) -> None:
        text = "## Out\n```bash\n## not a heading\n```\n~~~~\n### nor this\n```\n~~~~\n### Back\n"
        self.assertEqual(headings(text), ["## Out", "### Back"])

    def test_a_fence_closes_only_on_its_own_marker_at_least_as_long(self) -> None:
        self.assertEqual(headings("````\n```\n## in\n````\n## Out\n"), ["## Out"])
        self.assertEqual(headings("```\n~~~\n## in\n```\n## Out\n"), ["## Out"])

    def test_drops_a_closing_hash_sequence(self) -> None:
        self.assertEqual(headings("## Closed ##\n"), ["## Closed"])


class MapReaderTests(unittest.TestCase):
    def test_reads_rows_and_skips_comments(self) -> None:
        text = "# comment\n\n[a.md]\n## One\n  joined: tests/x.py\n### Two\n  prose: narrative: why\n\n[b.md]\n"
        self.assertEqual(
            parse_map(text),
            {"a.md": {"## One": ("joined", "tests/x.py"), "### Two": ("prose", "narrative: why")}, "b.md": {}},
        )

    def test_rejects_a_heading_with_no_classification(self) -> None:
        for text in ("[a.md]\n## One\n## Two\n  prose: x\n", "[a.md]\n## One\n"):
            with self.subTest(text=text), self.assertRaises(AssertionError):
                parse_map(text)

    def test_rejects_an_empty_value(self) -> None:
        with self.assertRaises(AssertionError):
            parse_map("[a.md]\n## One\n  prose:\n")

    def test_rejects_duplicates(self) -> None:
        for text in ("[a.md]\n[a.md]\n", "[a.md]\n## One\n  prose: x\n## One\n  prose: y\n"):
            with self.subTest(text=text), self.assertRaises(AssertionError):
                parse_map(text)

    def test_rejects_a_line_it_cannot_place(self) -> None:
        for text in ("## One\n  prose: x\n", "[a.md]\n  prose: x\n", "[a.md]\n## One\njoined: tests/x.py\n"):
            with self.subTest(text=text), self.assertRaises(AssertionError):
                parse_map(text)


class OpensPageTests(unittest.TestCase):
    def test_a_path_in_code_opens_the_page(self) -> None:
        for text in ('DOC = ROOT / "docs/using.md"\n', 'DOC = ROOT / "docs" / "using.md"\n'):
            with self.subTest(text=text):
                self.assertTrue(opens_page(text, ".py", "docs/using.md"))

    def test_a_docstring_or_comment_does_not(self) -> None:
        text = '"""Reads docs/using.md."""\n# docs/using.md\ndef f():\n    """using.md"""\n'
        self.assertFalse(opens_page(text, ".py", "docs/using.md"))

    def test_a_shell_comment_does_not(self) -> None:
        self.assertFalse(opens_page("# reads docs/using.md\ntrue\n", ".sh", "docs/using.md"))
        self.assertTrue(opens_page('doc="docs/using.md"\n', ".sh", "docs/using.md"))


def page_mismatch(rows: dict[str, dict[str, tuple[str, str]]], pages: set[str]) -> tuple[list[str], list[str]]:
    """(tracked pages with no block, blocks for pages that are not tracked)."""
    return sorted(pages - set(rows)), sorted(set(rows) - pages)


def unclassified(rows: dict[str, dict[str, tuple[str, str]]], root: Path) -> dict[str, list[str]]:
    """Page -> headings it has that the map gives no row."""
    found = {}
    for page, page_rows in sorted(rows.items()):
        missing = [h for h in headings((root / page).read_text()) if h not in page_rows]
        if missing:
            found[page] = missing
    return found


def stale(rows: dict[str, dict[str, tuple[str, str]]], root: Path) -> dict[str, list[str]]:
    """Page -> rows naming headings the page no longer has."""
    found = {}
    for page, page_rows in sorted(rows.items()):
        gone = sorted(set(page_rows) - set(headings((root / page).read_text())))
        if gone:
            found[page] = gone
    return found


def bad_join(page: str, heading: str, test: str, root: Path) -> str | None:
    """Why a `joined:` row does not hold, or None when it does."""
    if test == THIS_TEST:
        return "this test reads headings, not what a section says"
    path = root / test
    if not (test.startswith("tests/") and path.is_file()):
        return f"{page} {heading!r} is joined to {test}, which is not a file under tests/"
    if not opens_page(path.read_text(), path.suffix, page):
        return f"{page} {heading!r} is joined to {test}, which never opens {page}"
    return None


class GateOnFixtureTests(unittest.TestCase):
    """Each check fires on a small repository built for it."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        (self.root / "tests").mkdir()
        (self.root / "page.md").write_text("# Page\n## Kept\n## New\n")
        (self.root / "tests/test_reads.py").write_text('DOC = "page.md"\n')
        (self.root / "elsewhere.py").write_text('DOC = "page.md"\n')

    def test_a_heading_with_no_row_is_reported(self) -> None:
        rows = {"page.md": {"## Kept": ("prose", "x")}}
        self.assertEqual(unclassified(rows, self.root), {"page.md": ["## New"]})

    def test_a_row_for_a_missing_heading_is_reported(self) -> None:
        rows = {"page.md": {"## Kept": ("prose", "x"), "## New": ("prose", "x"), "## Gone": ("prose", "x")}}
        self.assertEqual(stale(rows, self.root), {"page.md": ["## Gone"]})
        self.assertEqual(unclassified(rows, self.root), {})

    def test_a_join_outside_tests_or_to_this_test_is_refused(self) -> None:
        self.assertIsNone(bad_join("page.md", "## Kept", "tests/test_reads.py", self.root))
        self.assertIn("not a file under tests/", bad_join("page.md", "## Kept", "elsewhere.py", self.root))
        self.assertIn("not a file under tests/", bad_join("page.md", "## Kept", "tests/absent.py", self.root))
        self.assertIn("reads headings", bad_join("page.md", "## Kept", THIS_TEST, self.root))

    def test_a_join_to_a_test_that_never_opens_the_page_is_refused(self) -> None:
        (self.root / "tests/test_other.py").write_text('"""Reads page.md."""\nDOC = "other.md"\n')
        self.assertIn("never opens", bad_join("page.md", "## Kept", "tests/test_other.py", self.root))

    def test_the_page_lists_compare_both_ways(self) -> None:
        self.assertEqual(page_mismatch({"a.md": {}, "b.md": {}}, {"a.md", "c.md"}), (["c.md"], ["b.md"]))


class SectionMapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.rows = parse_map(MAP.read_text())

    def test_the_map_lists_exactly_the_tracked_pages(self) -> None:
        unlisted, untracked = page_mismatch(self.rows, tracked_pages())
        self.assertEqual(
            unlisted,
            [],
            f"tracked pages with no [page] block in {MAP_RELATIVE}; add one with a row per heading",
        )
        self.assertEqual(
            untracked,
            [],
            f"{MAP_RELATIVE} lists pages that are not tracked documentation pages",
        )

    def test_every_heading_has_a_row(self) -> None:
        self.assertEqual(
            unclassified(self.rows, ROOT),
            {},
            f"sections {MAP_RELATIVE} does not classify; give each a "
            "joined: line naming the test that reads it, or a prose: line saying why none does",
        )

    def test_every_row_names_a_heading_the_page_has(self) -> None:
        self.assertEqual(
            stale(self.rows, ROOT),
            {},
            f"{MAP_RELATIVE} classifies headings the page no longer has; rename or drop the rows",
        )

    def test_every_joined_test_exists_and_opens_the_page(self) -> None:
        for page, rows in sorted(self.rows.items()):
            for heading, (kind, test) in rows.items():
                if kind != "joined":
                    continue
                with self.subTest(page=page, heading=heading):
                    self.assertIsNone(bad_join(page, heading, test, ROOT))


if __name__ == "__main__":
    unittest.main()

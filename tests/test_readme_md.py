"""Tests for tests/_readme_md.py, the parser the README tests rely on.

A parser used as a test oracle is only worth what its own failures are worth:
one that quietly accepted a malformed document, guessed at a construct it does
not really support, or handed back an empty result for a section that is no
longer there would make every assertion built on it weaker than the substring
assertions it replaced. So every rejection path has a test, and so does every
accessor that is supposed to raise rather than return nothing.
"""

import unittest

from _readme_md import ReadmeError
from _readme_md import parse as parse_readme

DOCUMENT = """# Custom Image

A description.

| Setting | Value |
|---------|-------|
| Repository | `example/test-image` |
| Base Image | `Bazzite` |

## Requested Packages

These are the packages.
And a second line of the same paragraph.

- `tmux`
- `ripgrep`

## Before The First Switch

The first build publishes

    ghcr.io/example/test-image:latest

Make it readable once:

1. Open the page
2. Change visibility

## Using The Image

```bash
sudo bootc switch ghcr.io/example/test-image:latest
systemctl reboot
```
"""


class ReadmeParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.doc = parse_readme(DOCUMENT)

    def test_reads_the_heading_outline_with_levels_and_order(self) -> None:
        self.assertEqual(
            self.doc.outline(),
            (
                (1, "Custom Image"),
                (2, "Requested Packages"),
                (2, "Before The First Switch"),
                (2, "Using The Image"),
            ),
        )

    def test_a_section_holds_the_blocks_under_its_own_heading_only(self) -> None:
        # The h1 does not swallow the h2s below it: a block landing in the
        # wrong section is precisely what these tests exist to catch, and a
        # nesting model the document does not have would hide it.
        title = self.doc.section("Custom Image")
        self.assertEqual([block.line for block in title.blocks], [3, 5])
        self.assertEqual(title.paragraph().text, "A description.")

    def test_joins_a_paragraphs_soft_line_breaks(self) -> None:
        paragraph = self.doc.section("Requested Packages").paragraph()
        self.assertEqual(
            paragraph.lines,
            ("These are the packages.", "And a second line of the same paragraph."),
        )
        self.assertEqual(
            paragraph.text,
            "These are the packages. And a second line of the same paragraph.",
        )

    def test_reads_bullets_ordered_items_and_a_literal_block(self) -> None:
        self.assertEqual(self.doc.bullets("Requested Packages"), ("`tmux`", "`ripgrep`"))
        switch = self.doc.section("Before The First Switch")
        self.assertEqual(switch.ordered(), ("Open the page", "Change visibility"))
        self.assertEqual(switch.literal_block().lines, ("ghcr.io/example/test-image:latest",))

    def test_reads_a_fenced_block_with_its_info_string_and_line_order(self) -> None:
        block = self.doc.section("Using The Image").code_block()
        self.assertEqual(block.info, "bash")
        self.assertEqual(
            block.lines,
            ("sudo bootc switch ghcr.io/example/test-image:latest", "systemctl reboot"),
        )
        self.assertEqual(self.doc.code_blocks("Using The Image"), (block,))

    def test_reads_a_table_by_row_label(self) -> None:
        table = self.doc.table("Custom Image")
        self.assertEqual(table.header, ("Setting", "Value"))
        self.assertEqual(table.labels, ("Repository", "Base Image"))
        self.assertEqual(table.value("Repository"), "`example/test-image`")
        self.assertEqual(table.value("Base Image", column="Value"), "`Bazzite`")

    def test_a_document_with_no_trailing_newline_parses_the_same(self) -> None:
        self.assertEqual(parse_readme("# A\n\nb\n"), parse_readme("# A\n\nb"))

    def test_an_empty_document_has_no_blocks(self) -> None:
        self.assertEqual(parse_readme("").blocks, ())

    # ── the accessors raise rather than return nothing ──────────────────

    def test_an_absent_section_is_an_error_not_an_empty_result(self) -> None:
        with self.assertRaisesRegex(ReadmeError, "no heading 'Enabled Services'"):
            self.doc.section("Enabled Services")

    def test_a_duplicated_section_is_an_error(self) -> None:
        doc = parse_readme("## Twice\n\na\n\n## Twice\n\nb\n")
        with self.assertRaisesRegex(ReadmeError, "appears 2 times"):
            doc.section("Twice")

    def test_a_section_with_no_block_of_the_kind_asked_for_is_an_error(self) -> None:
        section = self.doc.section("Using The Image")
        for accessor in (section.paragraph, section.bullets, section.ordered, section.table, section.literal_block):
            with self.assertRaises(ReadmeError):
                accessor()

    def test_a_section_with_two_blocks_of_one_kind_is_an_error(self) -> None:
        # Ambiguity is a failure too: "the bullet list" has no answer once
        # there are two, and picking the first would hide the second.
        doc = parse_readme("## Two\n\n- a\n\nsplit\n\n- b\n")
        with self.assertRaisesRegex(ReadmeError, "has 2 bullet lists"):
            doc.section("Two").bullets()

    def test_an_absent_or_repeated_table_row_is_an_error(self) -> None:
        table = self.doc.table("Custom Image")
        with self.assertRaisesRegex(ReadmeError, "has no row 'Published Image'"):
            table.value("Published Image")
        repeated = parse_readme("| a | b |\n|---|---|\n| x | 1 |\n| x | 2 |\n").blocks[0]
        with self.assertRaisesRegex(ReadmeError, "has 2 rows labelled 'x'"):
            repeated.value("x")

    def test_a_bad_column_name_is_an_error(self) -> None:
        table = self.doc.table("Custom Image")
        with self.assertRaisesRegex(ReadmeError, "has 0 columns named 'Nope'"):
            table.value("Repository", column="Nope")
        repeated = parse_readme("| a | a |\n|---|---|\n| x | 1 |\n").blocks[0]
        with self.assertRaisesRegex(ReadmeError, "has 2 columns named 'a'"):
            repeated.value("x", column="a")

    def test_the_default_column_needs_a_two_column_table(self) -> None:
        wide = parse_readme("| a | b | c |\n|---|---|---|\n| x | 1 | 2 |\n").blocks[0]
        with self.assertRaisesRegex(ReadmeError, "has 3 columns, so value\\(\\) needs a column name"):
            wide.value("x")

    # ── everything outside the subset is refused ────────────────────────

    def test_rejects_a_tab_anywhere(self) -> None:
        with self.assertRaisesRegex(ReadmeError, "tab character"):
            parse_readme("# A\n\n\tindented\n")

    def test_rejects_trailing_whitespace(self) -> None:
        # Two trailing spaces are a hard line break, which this parser does
        # not model, so it refuses rather than misrepresent the document.
        with self.assertRaisesRegex(ReadmeError, "trailing whitespace at line 3"):
            parse_readme("# A\n\nbroken  \nhere\n")

    def test_rejects_a_fence_that_is_never_closed(self) -> None:
        with self.assertRaisesRegex(ReadmeError, "never closed"):
            parse_readme("```bash\nsudo bootc switch x\n")

    def test_rejects_a_backtick_in_a_fence_info_string(self) -> None:
        with self.assertRaisesRegex(ReadmeError, "backtick in its info string"):
            parse_readme("````\nbody\n```\n")

    def test_rejects_a_heading_that_is_not_one(self) -> None:
        with self.assertRaisesRegex(ReadmeError, "has 7 '#'s"):
            parse_readme("####### too deep\n")
        with self.assertRaisesRegex(ReadmeError, "no space after its '#'s"):
            parse_readme("#NoSpace\n")
        with self.assertRaisesRegex(ReadmeError, "pads its text with spaces"):
            parse_readme("##  Padded\n")
        with self.assertRaisesRegex(ReadmeError, "closing '#' sequence"):
            parse_readme("## Title ##\n")

    def test_rejects_a_table_with_no_delimiter_row(self) -> None:
        with self.assertRaisesRegex(ReadmeError, "no delimiter row"):
            parse_readme("| a | b |\nnot a row\n")
        with self.assertRaisesRegex(ReadmeError, "no delimiter row"):
            parse_readme("| a | b |\n")

    def test_rejects_a_delimiter_row_that_does_not_match_the_header(self) -> None:
        with self.assertRaisesRegex(ReadmeError, "2 header cells but 3 delimiter cells"):
            parse_readme("| a | b |\n|---|---|---|\n")
        with self.assertRaisesRegex(ReadmeError, "is not a run of dashes"):
            parse_readme("| a | b |\n|---| b |\n")

    def test_rejects_a_body_row_whose_cell_count_disagrees_with_the_header(self) -> None:
        with self.assertRaisesRegex(ReadmeError, "has 3 cells but the header has 2"):
            parse_readme("| a | b |\n|---|---|\n| x | 1 | 2 |\n")

    def test_rejects_a_table_row_that_does_not_close_its_last_cell(self) -> None:
        with self.assertRaisesRegex(ReadmeError, "not delimited by"):
            parse_readme("| a | b\n|---|---|\n")
        with self.assertRaisesRegex(ReadmeError, "not delimited by"):
            parse_readme("|\n")

    def test_rejects_an_ordered_list_that_does_not_count_from_one(self) -> None:
        with self.assertRaisesRegex(ReadmeError, "numbers its item 1 as 2"):
            parse_readme("2. second\n3. third\n")
        with self.assertRaisesRegex(ReadmeError, "numbers its item 2 as 3"):
            parse_readme("1. first\n3. third\n")

    def test_rejects_an_indent_that_is_neither_a_paragraph_nor_a_literal_block(self) -> None:
        with self.assertRaisesRegex(ReadmeError, "indented 2 spaces"):
            parse_readme("# A\n\n  two spaces\n")

    def test_rejects_unsupported_markdown_block_openers(self) -> None:
        for opener in (
            "> block quote",
            "* unsupported bullet",
            "+ unsupported bullet",
            "---",
            "* * *",
            "___",
            "===",
            "--",
            "~~~python",
            "[label]: https://example.com",
        ):
            with self.subTest(opener=opener):
                with self.assertRaisesRegex(ReadmeError, "unsupported Markdown block opener at line 1"):
                    parse_readme(f"{opener}\n")

    def test_rejects_an_unsupported_block_opener_inside_a_paragraph(self) -> None:
        with self.assertRaisesRegex(ReadmeError, "unsupported Markdown block at line 2 follows the paragraph"):
            parse_readme("paragraph\n> block quote\n")

    def test_rejects_a_block_glued_to_the_one_above_it(self) -> None:
        # CommonMark reads some of these as lazy continuations of the block
        # above. The generator always leaves a blank line, so rather than pick
        # an interpretation the parser reports the missing break.
        for text in (
            "a paragraph\n## Heading\n",
            "a paragraph\n- item\n",
            "a paragraph\n| a | b |\n|---|---|\n",
            "a paragraph\n1. item\n",
            "a paragraph\n    literal\n",
            "a paragraph\n```bash\nx\n```\n",
            "- item\nnot an item\n",
            "1. item\nnot an item\n",
            "```bash\nx\n```\nglued\n",
            "| a | b |\n|---|---|\n| x | 1 |\nglued\n",
            "    literal\nglued\n",
        ):
            with self.subTest(text=text):
                with self.assertRaisesRegex(ReadmeError, "no blank line between them"):
                    parse_readme(text)


if __name__ == "__main__":
    unittest.main()

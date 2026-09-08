"""Tests for tests/_readme_md.py, the parser the README tests rely on.

A parser used as a test oracle is only worth what its own failures are worth:
one that quietly accepted a malformed document, guessed at a construct it does
not really support, or returned an empty section for a heading the generator
had stopped emitting would make every assertion built on it weaker than the
substring assertions it replaced.
"""

import unittest

from _readme_md import (
    BulletList,
    CodeBlock,
    Heading,
    LiteralBlock,
    OrderedList,
    Paragraph,
    ReadmeError,
    Table,
    parse,
)


class ReadmeParserTests(unittest.TestCase):
    def test_parses_every_construct_the_generator_emits(self) -> None:
        document = parse(
            "# Title\n"
            "\n"
            "A paragraph\n"
            "wrapped over two lines.\n"
            "\n"
            "| Setting | Value |\n"
            "|---------|-------|\n"
            "| Base Image | `Bazzite` |\n"
            "\n"
            "## Section\n"
            "\n"
            "- `tmux`\n"
            "- `ripgrep`\n"
            "\n"
            "    ghcr.io/example/test-image:latest\n"
            "\n"
            "1. First\n"
            "2. Second\n"
            "\n"
            "```bash\n"
            "sudo bootc switch ghcr.io/example/test-image:latest\n"
            "```\n"
        )
        self.assertEqual(
            [type(block).__name__ for block in document.blocks],
            [
                "Heading",
                "Paragraph",
                "Table",
                "Heading",
                "BulletList",
                "LiteralBlock",
                "OrderedList",
                "CodeBlock",
            ],
        )
        self.assertEqual(document.blocks[0], Heading(level=1, text="Title", line=1))
        # The wrapped paragraph is one block, so a test can assert on the
        # sentence rather than on whichever line it happened to break at.
        self.assertEqual(
            document.blocks[1], Paragraph(text="A paragraph\nwrapped over two lines.", line=3)
        )
        self.assertEqual(
            document.blocks[2],
            Table(header=("Setting", "Value"), rows=(("Base Image", "`Bazzite`"),), line=6),
        )
        self.assertEqual(document.blocks[4], BulletList(items=("`tmux`", "`ripgrep`"), line=12))
        self.assertEqual(
            document.blocks[5],
            LiteralBlock(lines=("ghcr.io/example/test-image:latest",), line=15),
        )
        self.assertEqual(document.blocks[6], OrderedList(items=("First", "Second"), line=17))
        self.assertEqual(
            document.blocks[7],
            CodeBlock(
                language="bash",
                lines=("sudo bootc switch ghcr.io/example/test-image:latest",),
                line=20,
            ),
        )

    def test_a_section_stops_at_the_next_heading_of_the_same_level(self) -> None:
        document = parse(
            "# Title\n"
            "\n"
            "## First\n"
            "\n"
            "- mine\n"
            "\n"
            "### Nested\n"
            "\n"
            "- also mine\n"
            "\n"
            "## Second\n"
            "\n"
            "- not mine\n"
        )
        self.assertEqual(
            [type(block).__name__ for block in document.section("First")],
            ["BulletList", "Heading", "BulletList"],
        )
        self.assertEqual(document.bullets("Nested"), ("also mine",))
        self.assertEqual(document.bullets("Second"), ("not mine",))
        # A level-1 heading owns everything under it, which is how a test can
        # tell "the document contains this" from "this section contains this".
        self.assertEqual(len(document.section("Title")), 6)

    def test_an_absent_or_duplicated_section_is_an_error_not_an_empty_body(self) -> None:
        document = parse("## One\n\n- a\n\n## One\n\n- b\n")
        with self.assertRaises(ReadmeError):
            document.section("Two")
        with self.assertRaises(ReadmeError):
            document.section("One")
        self.assertFalse(document.has_section("Two"))
        self.assertTrue(document.has_section("One"))

    def test_bullets_requires_exactly_one_list_in_the_section(self) -> None:
        document = parse("## Empty\n\n## Two\n\n- a\n\ntext\n\n- b\n")
        with self.assertRaises(ReadmeError):
            document.bullets("Empty")
        with self.assertRaises(ReadmeError):
            document.bullets("Two")

    def test_table_lookup_rejects_a_missing_or_repeated_label(self) -> None:
        table = parse(
            "| Setting | Value |\n"
            "|---------|-------|\n"
            "| Base Image | `one` |\n"
            "| Base Image | `two` |\n"
            "| Repository | `example/test-image` |\n"
        ).only_table()
        self.assertEqual(table.value("Repository"), "`example/test-image`")
        self.assertEqual(table.labels(), ("Base Image", "Base Image", "Repository"))
        with self.assertRaises(ReadmeError):
            table.value("Base Image")
        with self.assertRaises(ReadmeError):
            table.value("Published Image")

    def test_title_requires_exactly_one_level_one_heading(self) -> None:
        self.assertEqual(parse("# One\n\n## Two\n").title(), "One")
        with self.assertRaises(ReadmeError):
            parse("## Two\n").title()
        with self.assertRaises(ReadmeError):
            parse("# One\n\n# Other\n").title()

    def test_only_table_requires_exactly_one_table(self) -> None:
        with self.assertRaises(ReadmeError):
            parse("# Title\n").only_table()
        with self.assertRaises(ReadmeError):
            parse("| a |\n|---|\n\n| b |\n|---|\n").only_table()

    def test_rejects_an_unterminated_code_fence(self) -> None:
        with self.assertRaises(ReadmeError):
            parse("```bash\nsudo bootc switch x\n")

    def test_rejects_a_closing_fence_that_carries_a_language(self) -> None:
        with self.assertRaises(ReadmeError):
            parse("```bash\nsudo bootc switch x\n```bash\n")

    def test_rejects_a_malformed_heading(self) -> None:
        with self.assertRaises(ReadmeError):
            parse("#Title\n")
        with self.assertRaises(ReadmeError):
            parse("####### Too deep\n")

    def test_rejects_a_table_with_no_delimiter_row(self) -> None:
        with self.assertRaises(ReadmeError):
            parse("| Setting | Value |\n")
        with self.assertRaises(ReadmeError):
            parse("| Setting | Value |\n| Base Image | `x` |\n")
        with self.assertRaises(ReadmeError):
            parse("| Setting | Value |\n|---------|\n")

    def test_rejects_a_row_whose_cell_count_disagrees_with_the_header(self) -> None:
        with self.assertRaises(ReadmeError):
            parse("| Setting | Value |\n|---------|-------|\n| Base Image |\n")

    def test_rejects_a_row_that_is_not_closed_by_a_pipe(self) -> None:
        with self.assertRaises(ReadmeError):
            parse("| Setting | Value |\n|---------|-------|\n| Base Image | `x`\n")

    def test_rejects_an_ordered_list_that_does_not_count_from_one(self) -> None:
        with self.assertRaises(ReadmeError):
            parse("1. First\n1. Second\n")
        with self.assertRaises(ReadmeError):
            parse("2. First\n3. Second\n")

    def test_rejects_indentation_that_is_neither_a_literal_block_nor_prose(self) -> None:
        with self.assertRaises(ReadmeError):
            parse("  two spaces\n")

    def test_text_joins_only_the_paragraphs(self) -> None:
        document = parse("# Title\n\nprose\n\n- `tmux`\n\n```bash\ncode\n```\n\nmore prose\n")
        self.assertEqual(document.text(), "prose\nmore prose")


if __name__ == "__main__":
    unittest.main()

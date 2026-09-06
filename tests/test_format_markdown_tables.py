"""Unit tests for format_markdown_tables.py.

Until this file existed the formatter was exercised only as an oracle by
tests/test_atomic_image_builder.py's
test_markdown_tables_are_aligned_for_a_fixed_width_reader, which runs
format_text over the repo's own tracked Markdown and asserts nothing changes.
That proves the docs are aligned; it does not prove the formatter is right,
because the repo's Markdown happens not to contain an escaped pipe, a pipe
inside a code span, a centred or right-aligned delimiter, or a table that runs
to the last line of a file -- and it never calls main() at all. Those are the
paths asserted here.
"""

import contextlib
import io
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from format_markdown_tables import (
    SKIP_PREFIXES,
    delimiter_width,
    fence_closes,
    fence_open,
    format_text,
    is_delimiter,
    main,
    render_delimiter,
    split_row,
    tracked_markdown,
)

ROOT = Path(__file__).resolve().parents[1]


class SplitRowTests(unittest.TestCase):
    def test_line_without_a_pipe_is_not_a_row(self) -> None:
        self.assertIsNone(split_row("just prose"))

    def test_outer_pipes_are_dropped_so_both_styles_normalise_alike(self) -> None:
        self.assertEqual(split_row("| a | b |"), ["a", "b"])
        self.assertEqual(split_row("a | b"), ["a", "b"])

    def test_escaped_pipe_stays_inside_its_cell(self) -> None:
        # An escaped pipe is cell content. Treating it as a separator would
        # split the cell and rewrite the text, which corrupts the document
        # rather than merely misaligning it.
        self.assertEqual(split_row(r"| a \| b | c |"), [r"a \| b", "c"])

    def test_trailing_backslash_is_kept_verbatim(self) -> None:
        self.assertEqual(split_row("| a | b\\"), ["a", "b\\"])

    def test_pipe_inside_a_code_span_is_content_not_a_separator(self) -> None:
        self.assertEqual(split_row("| `a | b` | c |"), ["`a | b`", "c"])

    def test_a_row_of_only_empty_edge_cells_is_not_a_row(self) -> None:
        self.assertIsNone(split_row("|"))


class IsDelimiterTests(unittest.TestCase):
    def test_dashes_with_optional_colons_are_a_delimiter(self) -> None:
        self.assertTrue(is_delimiter(["---", ":--", "--:", ":-:"]))

    def test_empty_cells_and_text_are_not(self) -> None:
        self.assertFalse(is_delimiter([]))
        self.assertFalse(is_delimiter([""]))
        self.assertFalse(is_delimiter(["---", "a"]))
        # Colons alone carry no dash, so they are not a delimiter row.
        self.assertFalse(is_delimiter([":"]))


class DelimiterWidthTests(unittest.TestCase):
    def test_a_plain_delimiter_needs_three_dashes(self) -> None:
        self.assertEqual(delimiter_width("---"), 3)

    def test_an_anchored_delimiter_needs_only_its_colons_and_a_dash(self) -> None:
        # A one-sided anchor fits in two characters and a centred one in
        # three, so an anchored column is not forced as wide as a plain one.
        self.assertEqual(delimiter_width(":--"), 2)
        self.assertEqual(delimiter_width("--:"), 2)
        self.assertEqual(delimiter_width(":-:"), 3)


class RenderDelimiterTests(unittest.TestCase):
    def test_unaligned_delimiter_fills_the_column_width(self) -> None:
        self.assertEqual(render_delimiter("---", 6), "------")

    def test_unaligned_delimiter_never_renders_shorter_than_three(self) -> None:
        # Two dashes still parse, but three is the conventional minimum and
        # is what a narrow column has to widen to.
        self.assertEqual(render_delimiter("---", 1), "---")

    def test_centred_delimiter_keeps_both_colons(self) -> None:
        self.assertEqual(render_delimiter(":-:", 6), ":----:")
        self.assertEqual(render_delimiter(":-:", 1), ":-:")

    def test_right_aligned_delimiter_keeps_its_trailing_colon(self) -> None:
        self.assertEqual(render_delimiter("--:", 6), "-----:")
        self.assertEqual(render_delimiter("--:", 1), "-:")

    def test_left_aligned_delimiter_keeps_its_leading_colon(self) -> None:
        self.assertEqual(render_delimiter(":--", 6), ":-----")
        self.assertEqual(render_delimiter(":--", 1), ":-")


class FormatTextTests(unittest.TestCase):
    def test_cells_are_padded_to_the_widest_value_in_their_column(self) -> None:
        # The delimiter never renders shorter than three dashes, so a column
        # narrower than that widens to three -- every row of it, not just the
        # delimiter, or the closing pipes stop lining up.
        self.assertEqual(
            format_text("| a | bb |\n| --- | --- |\n| cccc | d |\n"),
            "| a    | bb  |\n| ---- | --- |\n| cccc | d   |\n",
        )

    def test_alignment_colons_survive_a_reflow(self) -> None:
        # Only the delimiter carries the alignment; cell text is left-padded
        # either way, because the renderer is what acts on the colons.
        self.assertEqual(
            format_text("| a | b | c |\n| :-- | :-: | --: |\n| dddd | eeee | ffff |"),
            "| a    | b    | c    |\n| :--- | :--: | ---: |\n| dddd | eeee | ffff |",
        )

    def test_prose_and_a_lone_pipe_line_are_left_alone(self) -> None:
        # A pipe line with no delimiter under it is not a table.
        text = "intro\n| a | b |\nmore prose\n"
        self.assertEqual(format_text(text), text)

    def test_a_table_inside_a_fence_is_left_verbatim(self) -> None:
        # Reformatting a fenced example would change what the example says.
        text = "```\n| a | b |\n| --- | --- |\n| cc | d |\n```\n"
        self.assertEqual(format_text(text), text)

    def test_a_tilde_fence_is_honoured_too(self) -> None:
        text = "~~~\n| a | b |\n| --- | --- |\n~~~\n"
        self.assertEqual(format_text(text), text)

    def test_a_fence_opening_immediately_after_a_table_ends_the_table(self) -> None:
        self.assertEqual(
            format_text("| a | bbbb |\n| --- | --- |\n```\nnot a row\n```\n"),
            "| a   | bbbb |\n| --- | ---- |\n```\nnot a row\n```\n",
        )

    def test_a_table_running_to_the_last_line_is_still_formatted(self) -> None:
        # No trailing newline, so the row loop runs out of lines rather than
        # meeting a non-row line -- the one way out of that loop the repo's
        # own Markdown never takes.
        self.assertEqual(
            format_text("| a | bb |\n| --- | --- |\n| cccc | d |"),
            "| a    | bb  |\n| ---- | --- |\n| cccc | d   |",
        )

    def test_a_body_row_wider_than_the_header_pads_the_delimiter(self) -> None:
        # Malformed Markdown already. Padding keeps the table valid and makes
        # the stray cell visible instead of dropping it or emitting an empty
        # delimiter cell, which would stop the table rendering at all.
        self.assertEqual(
            format_text("| a |\n| --- |\n| b | extra |\n"),
            "| a   |       |\n| --- | ----- |\n| b   | extra |\n",
        )

    def test_a_trailing_pipe_only_line_ends_the_table(self) -> None:
        self.assertEqual(
            format_text("| a | bbbb |\n| --- | --- |\n|\n"),
            "| a   | bbbb |\n| --- | ---- |\n|\n",
        )

    def test_padding_is_not_stripped_from_the_last_cell(self) -> None:
        # The trailing pad is what lines the closing pipes up, which is the
        # entire point of the tool in a fixed-width viewer.
        formatted = format_text("| a | b |\n| --- | --- |\n| c | dddd |\n")
        self.assertTrue(
            all(line.endswith(" |") for line in formatted.splitlines()),
            formatted,
        )

    def test_a_column_narrower_than_its_delimiter_widens_every_row(self) -> None:
        # Regression: the delimiter's three-dash minimum used to be applied to
        # the delimiter cell alone and left out of the column width, so a
        # single-character column rendered a delimiter row wider than every
        # other row -- and the result was a fixed point, so re-running the
        # formatter never repaired it and an idempotence check never saw it.
        formatted = format_text("| A | B |\n| --- | --- |\n| 1 | 2 |\n")
        self.assertEqual(
            formatted,
            "| A   | B   |\n| --- | --- |\n| 1   | 2   |\n",
        )
        self.assertEqual(
            {len(line) for line in formatted.splitlines()},
            {13},
            formatted,
        )

    def test_every_row_of_a_table_ends_at_the_same_column(self) -> None:
        # The invariant the tool exists to provide, asserted directly rather
        # than inferred from the output being unchanged. Mixed narrow and wide
        # columns, plain and anchored delimiters.
        formatted = format_text(
            "| a | long header | b |\n"
            "| --- | :-: | --: |\n"
            "| 1 | x | 2 |\n"
        )
        lengths = {len(line) for line in formatted.splitlines()}
        self.assertEqual(len(lengths), 1, formatted)

    def test_formatting_is_idempotent(self) -> None:
        once = format_text("| a | bb |\n| :-- | --: |\n| cccc | d |\n")
        self.assertEqual(format_text(once), once)

    def test_a_table_in_a_longer_fence_is_left_verbatim(self) -> None:
        # The shape this repo's own docs use to show a fenced example: an
        # outer four-backtick block whose body contains a bare ```. A boolean
        # fence flag toggles on that inner line, so the table below it read as
        # ordinary prose and was rewritten -- changing what the example says.
        text = "````markdown\n```\n| A | B |\n| --- | --- |\n| 1 | 2 |\n```\n````\n"
        self.assertEqual(format_text(text), text)

    def test_a_tilde_fence_is_not_closed_by_backticks(self) -> None:
        # Same failure from the other direction: a fence closes only on the
        # character it opened with.
        text = "~~~\n```\n| A | B |\n| --- | --- |\n```\n~~~\n"
        self.assertEqual(format_text(text), text)

    def test_an_indented_code_block_keeps_its_indentation(self) -> None:
        # split_row() strips indentation to find cells and the rows were
        # emitted at column zero, so a four-space code block came back as a
        # live table -- a change of rendered meaning, not just of spacing.
        text = "    | A | B |\n    | --- | --- |\n    | 1 | 2 |\n"
        self.assertEqual(format_text(text), text)

    def test_an_indented_code_block_ends_at_the_first_dedent(self) -> None:
        # The lines after it are ordinary Markdown again, so a real table
        # there still gets aligned.
        self.assertEqual(
            format_text("    | A | B |\n\n| C | D |\n| --- | --- |\n"),
            "    | A | B |\n\n| C   | D   |\n| --- | --- |\n",
        )

    def test_four_space_indentation_after_prose_is_not_a_code_block(self) -> None:
        # CommonMark only opens an indented code block where a paragraph could
        # start. Treating every four-space line as code would leave a lazily
        # indented table unaligned instead.
        self.assertEqual(
            format_text("intro\n    | A | B |\n    | --- | --- |\n"),
            "intro\n    | A   | B   |\n    | --- | --- |\n",
        )

    def test_a_table_nested_in_a_list_item_keeps_its_indentation(self) -> None:
        # Dedenting it to column zero moves the table out of the list item.
        self.assertEqual(
            format_text("- item\n\n  | A | B |\n  | --- | --- |\n  | 1 | 2 |\n"),
            "- item\n\n  | A   | B   |\n  | --- | --- |\n  | 1   | 2   |\n",
        )


class FenceTests(unittest.TestCase):
    def test_three_backticks_open_a_fence(self) -> None:
        self.assertEqual(fence_open("```"), ("`", 3))

    def test_an_info_string_is_allowed(self) -> None:
        self.assertEqual(fence_open("```python"), ("`", 3))

    def test_a_backtick_in_a_backtick_info_string_is_not_a_fence(self) -> None:
        # CommonMark forbids it, which is the rule that makes a ```markdown
        # line inside a four-backtick block content rather than an opener.
        self.assertIsNone(fence_open("```md ` inline"))

    def test_a_tilde_info_string_may_contain_backticks(self) -> None:
        self.assertEqual(fence_open("~~~ ` tick"), ("~", 3))

    def test_two_backticks_are_a_code_span_not_a_fence(self) -> None:
        self.assertIsNone(fence_open("``not a fence``"))

    def test_a_four_space_indent_cannot_open_a_fence(self) -> None:
        self.assertIsNone(fence_open("    ```"))

    def test_a_shorter_run_does_not_close_a_longer_fence(self) -> None:
        self.assertFalse(fence_closes("```", "`", 4))

    def test_an_equal_or_longer_run_closes_it(self) -> None:
        self.assertTrue(fence_closes("````", "`", 4))
        self.assertTrue(fence_closes("`````", "`", 4))

    def test_a_closing_fence_takes_no_info_string(self) -> None:
        self.assertFalse(fence_closes("``` still open", "`", 3))

    def test_a_closing_fence_may_be_indented_up_to_three_spaces(self) -> None:
        self.assertTrue(fence_closes("   ```", "`", 3))
        self.assertFalse(fence_closes("    ```", "`", 3))


class TrackedMarkdownTests(unittest.TestCase):
    def test_lists_tracked_markdown_and_skips_the_vendored_snapshots(self) -> None:
        listed = tracked_markdown(ROOT)
        names = {str(path.relative_to(ROOT)) for path in listed}
        self.assertIn("README.md", names)
        self.assertIn("CONTRIBUTING.md", names)
        self.assertTrue(
            all(not name.startswith(SKIP_PREFIXES) for name in names),
            sorted(name for name in names if name.startswith(SKIP_PREFIXES)),
        )
        # The skip has to actually exclude something, or it proves nothing.
        tracked = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "-z", "*.md", "*.mdc"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split("\0")
        self.assertTrue(
            [name for name in tracked if name.startswith(SKIP_PREFIXES)],
            "template_snapshots/ holds no tracked Markdown any more; the skip "
            "assertion above no longer proves anything",
        )
        self.assertTrue(all(path.is_absolute() for path in listed))


class MainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "doc.md"

    def run_main(self, argv: list[str]) -> tuple[int, str]:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(argv)
        return code, stdout.getvalue()

    def test_check_reports_an_unaligned_file_without_rewriting_it(self) -> None:
        original = "| a | bb |\n| --- | --- |\n| cccc | d |\n"
        self.path.write_text(original)
        code, output = self.run_main(["--check", str(self.path)])
        self.assertEqual(code, 1)
        self.assertIn(f"unaligned table: {self.path}", output)
        self.assertEqual(self.path.read_text(), original)

    def test_default_run_rewrites_the_file_and_names_it(self) -> None:
        self.path.write_text("| a | bb |\n| --- | --- |\n| cccc | d |\n")
        code, output = self.run_main([str(self.path)])
        self.assertEqual(code, 0)
        self.assertIn(f"aligned {self.path}", output)
        self.assertEqual(
            self.path.read_text(),
            "| a    | bb  |\n| ---- | --- |\n| cccc | d   |\n",
        )

    def test_an_already_aligned_file_is_neither_rewritten_nor_reported(self) -> None:
        aligned = "| a    | bb  |\n| ---- | --- |\n| cccc | d   |\n"
        self.path.write_text(aligned)
        before = self.path.stat().st_mtime_ns
        code, output = self.run_main([str(self.path)])
        self.assertEqual(code, 0)
        self.assertEqual(output, "")
        self.assertEqual(self.path.stat().st_mtime_ns, before)

    def test_check_with_no_paths_scans_the_repo_and_passes(self) -> None:
        # The no-arguments path is what a contributor and the docs actually
        # run, and it is the only caller of tracked_markdown() in main().
        # --check keeps it read-only: a plain run would rewrite the checkout.
        code, output = self.run_main(["--check"])
        self.assertEqual(code, 0, output)
        self.assertEqual(output, "")


class CoverageConfigTests(unittest.TestCase):
    def test_every_top_level_module_is_measured_by_the_unit_gate(self) -> None:
        # .coveragerc's source list is written out by hand, so a module added
        # to the repo root is invisible to the 90% gate until someone
        # remembers to add it here -- silently, because an unmeasured file
        # lowers no percentage. format_markdown_tables.py shipped that way.
        modules = {
            Path(name).stem
            for name in subprocess.run(
                ["git", "-C", str(ROOT), "ls-files", "-z", "*.py"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.split("\0")
            if name and "/" not in name
        }
        config = (ROOT / ".coveragerc").read_text()
        listed = set(
            re.findall(
                r"^\s+(\S+)$",
                re.split(r"^\[|^\w+ =", config.split("source =", 1)[1], maxsplit=1)[0],
                flags=re.MULTILINE,
            )
        )
        self.assertEqual(
            modules - listed,
            set(),
            "these root modules are not in .coveragerc's source list, so the "
            "unit gate does not measure them",
        )


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Align GitHub-flavoured Markdown tables so they read in a fixed-width viewer.

A table that renders fine in a browser is often unreadable in a terminal, a
pager, or a diff -- which is where most of this repo's documentation is
actually read. This pads every cell to its column width so the pipes line up.

Run with no arguments to fix every tracked Markdown file; a test asserts the
result stays aligned. Vendored files under template_snapshots/ are skipped:
they are a pinned upstream copy and reformatting them is a defect regardless
of how they look.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

SKIP_PREFIXES = ("template_snapshots/",)
FENCE_CHARS = ("`", "~")
# CommonMark's threshold for an indented code block, and the indentation at
# which a line can no longer open or close a fence.
CODE_INDENT = 4
# CommonMark reads a tab as advancing to the next multiple of four columns, so
# a single tab is already enough indentation for a code block.
TAB_STOP = 4
# A list item marker: a bullet, or one to nine digits and a period or paren,
# followed by a space or the end of the line. Matched against tab-expanded
# text. Without the space it is ordinary text -- "-x" and "1.5" are not items.
LIST_MARKER = re.compile(r"(?:[-+*]|\d{1,9}[.)])(?= |$)")
# A line of three or more "-", "*" or "_", spaced or not, is a thematic break,
# which takes precedence over a list item: "- - -" is a rule, not three items.
THEMATIC_BREAK = re.compile(r"([-*_])(?: *\1){2,} *$")
# Under a paragraph, a line of "=" or "-" turns it into a heading instead.
SETEXT_UNDERLINE = re.compile(r"(?:=+|-+) *$")
# An ATX heading or a blockquote: the other blocks a line carrying a pipe can
# open. List items, fences and HTML blocks are recognised separately.
BLOCK_START = re.compile(r"#{1,6}(?:[ \t]|$)|>")
# The HTML block tags (CommonMark type 6) as cmark-gfm, GitHub's renderer,
# lists them.
HTML_BLOCK_TAGS = (
    "address article aside base basefont blockquote body caption center col "
    "colgroup dd details dialog dir div dl dt fieldset figcaption figure footer "
    "form frame frameset h1 h2 h3 h4 h5 h6 head header hr html iframe legend li "
    "link main menu menuitem nav noframes ol optgroup option p param section "
    "source summary table tbody td tfoot th thead title tr track ul"
).split()
# Matches the line that ends a block of the last two types -- a blank one.
BLANK_LINE = re.compile(r"^\s*$")
# How each kind of HTML block (CommonMark types 1-6) starts, and what ends it.
# Everything in between is raw HTML, not Markdown, pipes or not.
HTML_BLOCKS = (
    (
        re.compile(r"<(?:script|pre|style|textarea)(?:[ \t>]|$)", re.IGNORECASE),
        re.compile(r"</(?:script|pre|style|textarea)>", re.IGNORECASE),
    ),
    (re.compile(r"<!--"), re.compile(r"-->")),
    (re.compile(r"<\?"), re.compile(r"\?>")),
    (re.compile(r"<![A-Za-z]"), re.compile(r">")),
    (re.compile(r"<!\[CDATA\["), re.compile(r"\]\]>")),
    (
        re.compile(r"</?(?:" + "|".join(HTML_BLOCK_TAGS) + r")(?:[ \t>]|/>|$)", re.IGNORECASE),
        BLANK_LINE,
    ),
)
# Type 7: any other complete tag alone on its line. Unlike the rest it cannot
# interrupt a paragraph, and a tag with text after it -- "<b>x</b> | y" -- is
# not one, so such a line is still a table row.
HTML_TAG_LINE = re.compile(
    r"(?:<[A-Za-z][A-Za-z0-9-]*(?:\s[^<>]*)?/?>|</[A-Za-z][A-Za-z0-9-]*\s*>)\s*$"
)


def html_block(text: str, paragraph: bool) -> re.Pattern[str] | None:
    """What ends the HTML block this text opens, or None if it opens none."""
    for start, end in HTML_BLOCKS:
        if start.match(text):
            return end
    if not paragraph and HTML_TAG_LINE.match(text):
        return BLANK_LINE
    return None


def backtick_run(text: str, start: int) -> int:
    """The length of the backtick run beginning at ``text[start]``."""
    end = start
    while end < len(text) and text[end] == "`":
        end += 1
    return end - start


def code_span_end(text: str, start: int, length: int) -> int | None:
    """Where a code span opened by ``length`` backticks ends, or None.

    ``start`` is the first character after the opening run. CommonMark closes
    a code span only on a backtick run of exactly the opening length, so a
    shorter or longer run inside it is content -- that is how ``` `` ` `` ```
    spells a literal backtick. Backslashes are literal inside a code span, so
    they are not skipped as escapes here.
    """
    index = start
    while index < len(text):
        if text[index] != "`":
            index += 1
            continue
        run = backtick_run(text, index)
        if run == length:
            return index + run
        index += run
    return None


def split_row(line: str) -> list[str] | None:
    """Split a table row into cells, or None if it is not a table row.

    Pipes inside backtick code spans and pipes escaped as ``\\|`` are cell
    content, not separators -- getting either wrong would corrupt the text
    rather than merely misalign it.

    A code span runs between backtick runs of equal length, and a run with no
    match is literal text. Toggling on every single backtick instead read
    ``` `` ` `` ``` as an open span that swallowed the next separator, so each
    pass merged two cells and appended another empty column.
    """
    stripped = line.strip()
    if "|" not in stripped:
        return None
    cells: list[str] = []
    current: list[str] = []
    index = 0
    while index < len(stripped):
        char = stripped[index]
        if char == "\\" and index + 1 < len(stripped):
            current.append(stripped[index : index + 2])
            index += 2
            continue
        if char == "`":
            run = backtick_run(stripped, index)
            end = code_span_end(stripped, index + run, run)
            stop = index + run if end is None else end
            current.append(stripped[index:stop])
            index = stop
            continue
        if char == "|":
            cells.append("".join(current))
            current = []
        else:
            current.append(char)
        index += 1
    cells.append("".join(current))
    # A leading or trailing pipe produces an empty edge cell; drop those so
    # tables written with and without outer pipes normalise the same way.
    if cells and not cells[0].strip():
        cells = cells[1:]
    if cells and not cells[-1].strip():
        cells = cells[:-1]
    return [cell.strip() for cell in cells] if cells else None


def is_delimiter(cells: list[str]) -> bool:
    return bool(cells) and all(
        cell and set(cell) <= set(":-") and "-" in cell for cell in cells
    )


def delimiter_width(cell: str) -> int:
    """The narrowest this delimiter cell can be drawn and still be one.

    Three dashes is the conventional minimum for a plain delimiter; an
    anchored one needs only its colons plus a dash. A column whose content is
    narrower than this has to widen to it -- every row of it, not just the
    delimiter, or the closing pipes stop lining up.
    """
    left = cell.startswith(":")
    right = cell.endswith(":")
    return 3 if not (left or right) else 1 + left + right


def render_delimiter(cell: str, width: int) -> str:
    left = cell.startswith(":")
    right = cell.endswith(":")
    inner = max(width, delimiter_width(cell))
    if left and right:
        return ":" + "-" * (inner - 2) + ":"
    if right:
        return "-" * (inner - 1) + ":"
    if left:
        return ":" + "-" * (inner - 1)
    return "-" * inner


def fence_open(line: str) -> tuple[str, int] | None:
    """The character and length of the code fence this line opens, or None.

    A fence closes only on the same character and at least the opening run's
    length, so tracking a boolean is not enough: a four-backtick block whose
    body contains a bare ``` is one block, and toggling on the inner line
    inverts the state for everything after it. The info-string rule matters
    for the same example -- a backtick info string may not itself contain a
    backtick, which is what makes ```markdown inside such a block content
    rather than a nested opener.

    Takes the line with its tabs expanded and cut at its container's content
    column, so the indentation checked is relative to the enclosing list item.
    """
    indent = leading_spaces(line)
    if indent >= CODE_INDENT:
        return None
    rest = line[indent:]
    if not rest.startswith(FENCE_CHARS):
        return None
    char = rest[0]
    length = len(rest) - len(rest.lstrip(char))
    if length < 3:
        return None
    if char == "`" and "`" in rest[length:]:
        return None
    return char, length


def fence_closes(line: str, char: str, length: int) -> bool:
    indent = leading_spaces(line)
    if indent >= CODE_INDENT:
        return False
    rest = line[indent:]
    run = len(rest) - len(rest.lstrip(char))
    return run >= length and not rest[run:].strip()


def leading_spaces(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def table_spans(lines: list[str]) -> list[range]:
    """The lines of every table, header through its last body row.

    The formatter rewrites exactly these lines and the repo-wide alignment
    check in the test suite measures exactly these, so the two cannot disagree
    about where a table is. They did once: the check knew only about fences,
    so a four-space indented example was a ragged table that the formatter
    correctly refused to touch -- a failure with no way to clear it.

    Everything else here exists to say what is *not* a table, which takes
    following enough of GitHub's block structure to get code and list items
    right. A mistake is a change of rendered meaning, not of spacing:

    * Tabs count to the next multiple of four columns, so a tab-indented
      example is an indented code block like a four-space one.
    * Indentation is measured from the enclosing list item's content column.
      A fence four spaces in under "- item" is two spaces into the item, so it
      opens; code under it is code. A non-blank line indented less than the
      column leaves the item -- and closes any fence inside it -- unless it
      lazily continues a paragraph.
    * A fence closes only on its own character at no less than its opening
      length. An indented block opens four columns past its container on any
      line that is not continuing a paragraph -- after a heading, a rule or a
      closed fence as much as after a blank line -- and runs to the first
      non-blank line that is not. An HTML block runs to its end condition.
    * A table ends at a line that is not a row, and at one that opens another
      block -- a list item, heading, blockquote, fence, HTML block or indented
      code -- even if that line has a pipe in it. The header may not open one
      either, and the header and delimiter sit under four columns into their
      container, with the same number of cells.

    Blockquotes are followed only as far as their lazy continuation lines. A
    table inside one carries a ">" that split_row() reads as a cell, so its
    delimiter never matches and the table is left alone rather than misread.
    """
    spans: list[range] = []
    items: list[int] = []  # content column of each open list item, outermost first
    fence: tuple[str, int] | None = None
    indented = False
    html: re.Pattern[str] | None = None  # what ends the open HTML block
    table: int | None = None  # first line of the open table
    # The previous line and its cell count, when it could head a table.
    header: tuple[int, int] | None = None
    paragraph = False  # the previous line was paragraph text this one can continue
    quoted = False  # ... and that paragraph is inside a blockquote
    tried = False  # ... and a delimiter row in it already failed to open a table
    empty_item = False  # the previous line opened a list item with nothing in it

    def close(end: int) -> None:
        nonlocal table
        if table is not None:
            spans.append(range(table, end))
            table = None

    for index, raw in enumerate(lines):
        line = raw.expandtabs(TAB_STOP)
        indent = leading_spaces(line)
        blank = indent == len(line)
        if empty_item and blank and indent < items[-1]:
            # An item may begin with at most one blank line, so an empty one
            # followed by a blank that does not reach its column has ended.
            items.pop()
        empty_item = False
        # How many open list items this line stays inside: a blank line stays
        # in all of them, any other only in those it reaches the column of.
        depth = len(items) if blank else sum(indent >= column for column in items)
        base = items[depth - 1] if depth else 0

        if fence is not None:
            if depth == len(items):
                if fence_closes(line[base:], *fence):
                    fence = None
                continue
            fence = None
        if indented:
            if blank or (depth == len(items) and indent - base >= CODE_INDENT):
                continue
            indented = False
        if html is not None:
            if not blank and depth < len(items):
                html = None
            elif html.search(line):
                html = None
                if not blank:
                    continue
            else:
                continue

        if not blank and paragraph and (quoted or depth < len(items)):
            rest = line[indent:]
            interrupts = indent - base < CODE_INDENT and (
                LIST_MARKER.match(rest)
                or BLOCK_START.match(rest)
                or THEMATIC_BREAK.match(rest)
                or fence_open(rest)
                or html_block(rest, False)
            )
            if not interrupts:
                # A lazy continuation line: still paragraph text inside every
                # open item or quote, whatever its indentation. It is never a
                # table row.
                header = None
                continue
            if not rest.startswith(">"):
                paragraph = False
        if depth < len(items):
            del items[depth:]
            close(index)
            header = None
            paragraph = False

        rest = line[indent:]
        if not blank and indent - base < CODE_INDENT and (
            (paragraph and SETEXT_UNDERLINE.match(rest)) or THEMATIC_BREAK.match(rest)
        ):
            # Ends a paragraph -- by making it a heading, for an underline --
            # so what follows can open a code block.
            close(index)
            header = None
            paragraph = False
            continue

        opened_item = False
        while not blank and indent - base < CODE_INDENT:
            marker = LIST_MARKER.match(line, indent)
            if marker is None:
                break
            end = marker.end()
            content = end + leading_spaces(line[end:])
            # A paragraph goes on through a line that would start an empty
            # item, or one numbered other than 1: neither may interrupt it.
            number = marker.group()[:-1]
            if paragraph and (content == len(line) or number.isdigit() and int(number) != 1):
                break
            # The item's content starts one column past the marker when
            # nothing follows it, and when five or more spaces do -- all but
            # one of those then indent a code block inside the item.
            if content == len(line) or content - end > CODE_INDENT:
                base = end + 1
            else:
                base = content
            items.append(base)
            indent, blank = content, content == len(line)
            opened_item = True
            paragraph = False
        if opened_item:
            close(index)
            header = None
            empty_item = blank
        if blank:
            close(index)
            header = None
            paragraph = False
            continue
        if indent - base >= CODE_INDENT and not paragraph:
            close(index)
            header = None
            indented = True
            continue
        opened = fence_open(line[base:])
        if opened is not None:
            close(index)
            header = None
            paragraph = False
            fence = opened
            continue

        rest = line[indent:]
        ends = html_block(rest, paragraph) if indent - base < CODE_INDENT else None
        if ends is not None:
            close(index)
            header = None
            paragraph = False
            html = None if ends.search(rest) else ends
            continue

        starts_block = indent - base < CODE_INDENT and BLOCK_START.match(rest) is not None
        # A list item's first line is never a row: "- | a |" would read the
        # marker as a cell. A table opening there is left alone.
        row = None if opened_item or starts_block else split_row(raw)
        if table is not None:
            if row:
                continue
            close(index)
        if not paragraph:
            tried = False
        delimiter = row is not None and indent - base < CODE_INDENT and is_delimiter(row)
        if delimiter and header is not None and header[1] == len(row) and not tried:
            table = header[0]
            header = None
            paragraph = False
            continue
        # GitHub tries each paragraph once: a delimiter row that fails to open
        # a table -- its header has a different cell count -- means no later
        # line of the same paragraph can open one either.
        tried = tried or (delimiter and paragraph)
        # A header indented four columns past its container is continuing a
        # paragraph. GitHub still reads it as the header, but it is left
        # alone: re-emitting the table there would make every row paragraph
        # text, and moving the header would change which blocks it closes.
        header = (index, len(row)) if row and indent - base < CODE_INDENT else None
        # A blockquote's own paragraph can be lazily continued by the lines
        # after it, which makes them its text rather than a table.
        quoted = starts_block and rest.startswith(">") and bool(rest[1:].strip())
        paragraph = quoted or not starts_block
    close(len(lines))
    return spans


def render_table(rows: list[str]) -> list[str]:
    """Rows padded so every one of them ends at the same column."""
    block = [split_row(row) or [] for row in rows]
    columns = max(len(row) for row in block)
    # A body row with more cells than the header is malformed markdown
    # already. Padding the delimiter out to match keeps the table valid
    # and makes the extra cell visible, rather than dropping content or
    # emitting an empty delimiter cell that breaks rendering.
    block = [row + [""] * (columns - len(row)) for row in block]
    block[1] = [cell or "---" for cell in block[1]]
    # The delimiter is row 1 by position -- both here and below.
    # Re-detecting it by content would fail once it has been padded.
    #
    # Its own minimum is part of the column width rather than a floor
    # applied to the delimiter cell alone: leave it out and a column
    # narrower than three renders its delimiter wider than every other
    # row, the exact misalignment this tool exists to remove. That
    # output is also a fixed point, so re-running never repairs it.
    widths = [
        max(
            max(len(row[column]) for i, row in enumerate(block) if i != 1),
            delimiter_width(block[1][column]),
        )
        for column in range(columns)
    ]
    # The header's own indentation, tabs included, so a table nested in a
    # list item stays in that list item. split_row() strips it to find the
    # cells, and emitting at column zero moved the table out of whatever
    # contained it. The header's rather than any other row's because the
    # header is the line that decides which list items and fences are still
    # open; moving it deeper can pull the table into a block it had closed.
    header = rows[0]
    prefix = header[: len(header) - len(header.lstrip(" \t"))]
    out: list[str] = []
    for i, row in enumerate(block):
        if i == 1:
            cells = [render_delimiter(row[c], widths[c]) for c in range(columns)]
        else:
            cells = [row[c].ljust(widths[c]) for c in range(columns)]
        # No rstrip: the trailing pad is what makes the closing pipes line
        # up, which is the entire point in a fixed-width viewer.
        out.append(prefix + "| " + " | ".join(cells) + " |")
    return out


def format_text(text: str) -> str:
    lines = text.split("\n")
    out: list[str] = []
    done = 0
    for span in table_spans(lines):
        out.extend(lines[done : span.start])
        out.extend(render_table(lines[span.start : span.stop]))
        done = span.stop
    out.extend(lines[done:])
    return "\n".join(out)


def tracked_markdown(root: Path) -> list[Path]:
    listing = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "*.md", "*.mdc"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    return [
        root / name
        for name in listing
        if name and not name.startswith(SKIP_PREFIXES)
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument("--check", action="store_true", help="Report, do not rewrite.")
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parent
    paths = args.paths or tracked_markdown(root)
    unaligned: list[Path] = []
    for path in paths:
        original = path.read_text()
        formatted = format_text(original)
        if formatted == original:
            continue
        unaligned.append(path)
        if not args.check:
            path.write_text(formatted)
    if args.check:
        for path in unaligned:
            print(f"unaligned table: {path}")
        return 1 if unaligned else 0
    for path in unaligned:
        print(f"aligned {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

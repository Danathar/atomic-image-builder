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
import subprocess
import sys
from pathlib import Path

SKIP_PREFIXES = ("template_snapshots/",)
FENCE_CHARS = ("`", "~")
# CommonMark's threshold for an indented code block, and the indentation at
# which a line can no longer open or close a fence.
CODE_INDENT = 4


def split_row(line: str) -> list[str] | None:
    """Split a table row into cells, or None if it is not a table row.

    Pipes inside backtick code spans and pipes escaped as ``\\|`` are cell
    content, not separators -- getting either wrong would corrupt the text
    rather than merely misalign it.
    """
    stripped = line.strip()
    if "|" not in stripped:
        return None
    cells: list[str] = []
    current: list[str] = []
    in_code = False
    index = 0
    while index < len(stripped):
        char = stripped[index]
        if char == "\\" and index + 1 < len(stripped):
            current.append(stripped[index : index + 2])
            index += 2
            continue
        if char == "`":
            in_code = not in_code
        if char == "|" and not in_code:
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


def format_text(text: str) -> str:
    lines = text.split("\n")
    out: list[str] = []
    index = 0
    fence: tuple[str, int] | None = None
    in_code_block = False
    # An indented code block may open at the very start of a document, so the
    # notional line before it counts as blank.
    after_blank = True
    while index < len(lines):
        line = lines[index]
        if fence is not None:
            out.append(line)
            if fence_closes(line, *fence):
                fence = None
            index += 1
            after_blank = False
            continue

        stripped = line.strip()
        indent = leading_spaces(line)
        if stripped:
            # A blank line neither opens an indented code block nor closes
            # one; only a non-blank line at less than four spaces ends it.
            in_code_block = indent >= CODE_INDENT and (in_code_block or after_blank)
        after_blank = not stripped

        if in_code_block:
            out.append(line)
            index += 1
            continue

        opened = fence_open(line)
        if opened is not None:
            fence = opened
            out.append(line)
            index += 1
            continue

        header = split_row(line)
        delimiter = split_row(lines[index + 1]) if index + 1 < len(lines) else None
        if not header or not delimiter or not is_delimiter(delimiter):
            out.append(line)
            index += 1
            continue

        block = [header, delimiter]
        cursor = index + 2
        while cursor < len(lines):
            row = split_row(lines[cursor])
            if not row or fence_open(lines[cursor]) is not None:
                break
            block.append(row)
            cursor += 1

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
        # The header's own indentation, so a table nested in a list item stays
        # in that list item. split_row() strips it to find the cells, and
        # emitting at column zero moved the table out of whatever contained it.
        prefix = line[:indent]
        for i, row in enumerate(block):
            if i == 1:
                cells = [render_delimiter(row[c], widths[c]) for c in range(columns)]
            else:
                cells = [row[c].ljust(widths[c]) for c in range(columns)]
            # No rstrip: the trailing pad is what makes the closing pipes line
            # up, which is the entire point in a fixed-width viewer.
            out.append(prefix + "| " + " | ".join(cells) + " |")
        index = cursor
        after_blank = False
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

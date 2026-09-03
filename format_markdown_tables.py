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
FENCES = ("```", "~~~")


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


def render_delimiter(cell: str, width: int) -> str:
    left = cell.startswith(":")
    right = cell.endswith(":")
    inner = max(width, 3 if not (left or right) else 1 + left + right)
    if left and right:
        return ":" + "-" * (inner - 2) + ":"
    if right:
        return "-" * (inner - 1) + ":"
    if left:
        return ":" + "-" * (inner - 1)
    return "-" * inner


def format_text(text: str) -> str:
    lines = text.split("\n")
    out: list[str] = []
    index = 0
    in_fence = False
    while index < len(lines):
        line = lines[index]
        if line.lstrip().startswith(FENCES):
            in_fence = not in_fence
            out.append(line)
            index += 1
            continue
        if in_fence:
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
            if not row or lines[cursor].lstrip().startswith(FENCES):
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
        # The delimiter is row 1 by position. Re-detecting it by content
        # would fail once it has been padded.
        widths = [
            max(len(row[column]) for i, row in enumerate(block) if i != 1)
            for column in range(columns)
        ]
        for i, row in enumerate(block):
            if i == 1:
                cells = [render_delimiter(row[c], widths[c]) for c in range(columns)]
            else:
                cells = [row[c].ljust(widths[c]) for c in range(columns)]
            # No rstrip: the trailing pad is what makes the closing pipes line
            # up, which is the entire point in a fixed-width viewer.
            out.append("| " + " | ".join(cells) + " |")
        index = cursor
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

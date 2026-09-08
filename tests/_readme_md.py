"""A strict parser for the Markdown subset ``generate_readme`` emits.

``generate_readme`` builds the project README by joining string literals into
one list and returning ``"\\n".join(sections)``, and the tests for it asserted
substring membership only. That leaves the document's structure unasserted,
and the structure is where the meaning is: which heading a list of names sits
under, which row of the settings table a value lands in, what order the
commands inside a fenced block run in, whether a section body was replaced by
another section's body.

Every one of these produces a wrong README while every ``assertIn`` in the
suite still passes:

* ``services`` rendered under ``## COPR Repositories`` and ``copr_repos``
  under ``## Enabled Services`` -- both names are still "in" the document.
* ``| Base Image URI |`` filled with the base image's display name instead of
  its URI -- the URI is still "in" the document, in the row above.
* ``systemctl reboot`` placed between ``rpm-ostree reset`` and
  ``bootc switch``, so following the README reboots mid-transaction.
* The configured image description dropped from the top of the file.
* ``## Removed Base Packages`` listing the requested packages.

Substrings cannot see any of it, because a substring assertion asks only
whether a string appears somewhere in a 100-line document. So this parses the
README into blocks instead, and gives the tests a way to ask what is under a
given heading rather than what is somewhere in the file.

CI installs no third-party packages for the unit suite (``coverage`` and
``ruff``, pinned in CONTRIBUTING.md), so this is stdlib-only. It handles only
what ``generate_readme`` can produce -- ATX headings, paragraphs, bullet
lists, ordered lists, pipe tables, fenced code blocks and four-space literal
blocks -- and raises :class:`ReadmeError` on anything else, including an
unterminated fence, a table row whose cell count disagrees with its header,
and an ordered list that does not count from 1. Being narrow is the point: an
unsupported construct is a failure, never a guess.

Not collected as a test module (name doesn't start with ``test``), but
importable by files under tests/ once ``unittest discover -s tests`` puts this
directory on sys.path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


class ReadmeError(Exception):
    """Raised for any construct this parser does not support."""


# A fenced block opens and closes with exactly three backticks; the opening
# fence may name a language. Anything else (tildes, four backticks, an info
# string with spaces) is outside what the generator emits.
_FENCE = re.compile(r"^```([A-Za-z0-9_+-]*)$")
_HEADING = re.compile(r"^(#{1,6}) +(\S.*)$")
_BULLET = re.compile(r"^- +(\S.*)$")
_ORDERED = re.compile(r"^(\d+)\. +(\S.*)$")
_TABLE_DELIMITER_CELL = re.compile(r"^:?-{3,}:?$")


@dataclass(frozen=True)
class Heading:
    level: int
    text: str
    line: int


@dataclass(frozen=True)
class Paragraph:
    text: str
    line: int


@dataclass(frozen=True)
class BulletList:
    items: tuple[str, ...]
    line: int


@dataclass(frozen=True)
class OrderedList:
    items: tuple[str, ...]
    line: int


@dataclass(frozen=True)
class Table:
    header: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    line: int

    def value(self, label: str) -> str:
        """Return the second cell of the row whose first cell is ``label``.

        Raises rather than returning a default: a settings table that lost a
        row, or grew a second row with the same label, is a defect in the
        generated README and must not read as an empty value.
        """
        matches = [row for row in self.rows if row[0] == label]
        if not matches:
            raise ReadmeError(f"table has no row labelled {label!r}")
        if len(matches) > 1:
            raise ReadmeError(f"table has {len(matches)} rows labelled {label!r}")
        (row,) = matches
        if len(row) != 2:
            raise ReadmeError(f"row {label!r} has {len(row)} cells, expected 2")
        return row[1]

    def labels(self) -> tuple[str, ...]:
        """First-column cells, in document order."""
        return tuple(row[0] for row in self.rows)


@dataclass(frozen=True)
class CodeBlock:
    language: str
    lines: tuple[str, ...]
    line: int


@dataclass(frozen=True)
class LiteralBlock:
    """A four-space-indented block, Markdown's other way of showing code."""

    lines: tuple[str, ...]
    line: int


@dataclass(frozen=True)
class Document:
    blocks: tuple[object, ...]

    def headings(self) -> tuple[Heading, ...]:
        return tuple(block for block in self.blocks if isinstance(block, Heading))

    def heading_texts(self) -> tuple[str, ...]:
        return tuple(heading.text for heading in self.headings())

    def title(self) -> str:
        """The single level-1 heading.

        A README with none, or with two, is malformed regardless of what the
        rest of it says.
        """
        top = [heading for heading in self.headings() if heading.level == 1]
        if len(top) != 1:
            raise ReadmeError(f"expected exactly 1 level-1 heading, found {len(top)}")
        return top[0].text

    def section(self, title: str) -> tuple[object, ...]:
        """Blocks under ``title``, up to the next heading of the same or a
        higher level.

        An absent or duplicated heading raises: a test that asked for a
        section the generator stopped emitting must fail, not silently assert
        against an empty body.
        """
        starts = [
            index
            for index, block in enumerate(self.blocks)
            if isinstance(block, Heading) and block.text == title
        ]
        if not starts:
            raise ReadmeError(f"no heading titled {title!r}")
        if len(starts) > 1:
            raise ReadmeError(f"{len(starts)} headings titled {title!r}")
        (start,) = starts
        level = self.blocks[start].level  # type: ignore[attr-defined]
        end = len(self.blocks)
        for index in range(start + 1, len(self.blocks)):
            block = self.blocks[index]
            if isinstance(block, Heading) and block.level <= level:
                end = index
                break
        return self.blocks[start + 1 : end]

    def has_section(self, title: str) -> bool:
        return any(
            isinstance(block, Heading) and block.text == title for block in self.blocks
        )

    def bullets(self, title: str) -> tuple[str, ...]:
        """The items of the one bullet list under ``title``."""
        lists = [block for block in self.section(title) if isinstance(block, BulletList)]
        if len(lists) != 1:
            raise ReadmeError(f"section {title!r} has {len(lists)} bullet lists, expected 1")
        return lists[0].items

    def code_blocks(self, title: str) -> tuple[CodeBlock, ...]:
        return tuple(
            block for block in self.section(title) if isinstance(block, CodeBlock)
        )

    def only_table(self) -> Table:
        tables = [block for block in self.blocks if isinstance(block, Table)]
        if len(tables) != 1:
            raise ReadmeError(f"document has {len(tables)} tables, expected 1")
        return tables[0]

    def text(self) -> str:
        """Every paragraph, joined -- for the few assertions that really are
        about prose rather than structure."""
        return "\n".join(
            block.text for block in self.blocks if isinstance(block, Paragraph)
        )


def _split_row(raw: str, line: int) -> tuple[str, ...]:
    if not raw.startswith("|") or not raw.endswith("|"):
        raise ReadmeError(f"line {line}: table row is not delimited by pipes: {raw!r}")
    return tuple(cell.strip() for cell in raw[1:-1].split("|"))


def parse(text: str) -> Document:
    """Parse ``text`` into a :class:`Document`, or raise :class:`ReadmeError`."""
    lines = text.split("\n")
    blocks: list[object] = []
    index = 0
    while index < len(lines):
        raw = lines[index]
        number = index + 1
        if not raw.strip():
            index += 1
            continue

        fence = _FENCE.match(raw)
        if fence:
            body: list[str] = []
            index += 1
            while index < len(lines) and not _FENCE.match(lines[index]):
                body.append(lines[index])
                index += 1
            if index >= len(lines):
                raise ReadmeError(f"line {number}: unterminated code fence")
            closing = _FENCE.match(lines[index])
            assert closing is not None
            if closing.group(1):
                raise ReadmeError(f"line {index + 1}: closing fence carries a language")
            index += 1
            blocks.append(CodeBlock(language=fence.group(1), lines=tuple(body), line=number))
            continue

        if raw.startswith("#"):
            heading = _HEADING.match(raw)
            if not heading:
                raise ReadmeError(f"line {number}: malformed heading: {raw!r}")
            blocks.append(
                Heading(level=len(heading.group(1)), text=heading.group(2).rstrip(), line=number)
            )
            index += 1
            continue

        if _BULLET.match(raw):
            items = []
            while index < len(lines):
                item = _BULLET.match(lines[index])
                if not item:
                    break
                items.append(item.group(1).rstrip())
                index += 1
            blocks.append(BulletList(items=tuple(items), line=number))
            continue

        if _ORDERED.match(raw):
            items = []
            expected = 1
            while index < len(lines):
                item = _ORDERED.match(lines[index])
                if not item:
                    break
                if int(item.group(1)) != expected:
                    raise ReadmeError(
                        f"line {index + 1}: ordered list item numbered "
                        f"{item.group(1)}, expected {expected}"
                    )
                items.append(item.group(2).rstrip())
                expected += 1
                index += 1
            blocks.append(OrderedList(items=tuple(items), line=number))
            continue

        if raw.startswith("|"):
            header = _split_row(raw, number)
            if index + 1 >= len(lines):
                raise ReadmeError(f"line {number}: table header has no delimiter row")
            delimiter = _split_row(lines[index + 1], index + 2)
            if len(delimiter) != len(header) or not all(
                _TABLE_DELIMITER_CELL.match(cell) for cell in delimiter
            ):
                raise ReadmeError(f"line {index + 2}: table delimiter row is malformed")
            index += 2
            rows = []
            while index < len(lines) and lines[index].startswith("|"):
                row = _split_row(lines[index], index + 1)
                if len(row) != len(header):
                    raise ReadmeError(
                        f"line {index + 1}: table row has {len(row)} cells, "
                        f"header has {len(header)}"
                    )
                rows.append(row)
                index += 1
            blocks.append(Table(header=header, rows=tuple(rows), line=number))
            continue

        if raw.startswith("    ") and raw.strip():
            body = []
            while index < len(lines) and lines[index].startswith("    "):
                body.append(lines[index][4:])
                index += 1
            blocks.append(LiteralBlock(lines=tuple(body), line=number))
            continue

        if raw.startswith(" "):
            raise ReadmeError(f"line {number}: unsupported indentation: {raw!r}")

        paragraph = []
        while index < len(lines):
            current = lines[index]
            if not current.strip():
                break
            if (
                current.startswith("#")
                or current.startswith("|")
                or current.startswith(" ")
                or _FENCE.match(current)
                or _BULLET.match(current)
                or _ORDERED.match(current)
            ):
                break
            paragraph.append(current)
            index += 1
        blocks.append(Paragraph(text="\n".join(paragraph), line=number))

    return Document(blocks=tuple(blocks))

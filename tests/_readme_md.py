"""A strict parser for the Markdown subset ``generate_readme`` emits.

``generate_readme`` builds the generated project README by joining ~90 string
literals into one list and returning ``"\\n".join(sections)``, and every test
for it asserted substring membership against that flat string. A substring
assertion only asks whether some text appears *somewhere* in a hundred-line
document, and the meaning of this document lives in its structure: which
heading a list of names sits under, which row of the settings table a value
lands in, and what order the commands inside a fenced block run in.

Nine single-edit mutations to the generator survived the whole unit suite for
exactly that reason -- ``services`` rendered under ``## COPR Repositories``
and ``copr_repos`` under ``## Enabled Services`` (both lists still "in" the
document), the ``| Base Image URI |`` row filled from ``base_name`` (the URI
still "in" it, one row up), the configured description paragraph dropped
outright, ``## Managed By`` renamed to ``## Managed by``, and ``systemctl
reboot`` moved to sit between ``rpm-ostree reset`` and ``bootc switch``, which
turns the README into an instruction to reboot mid-transaction that the prose
two lines below it warns against. So this parses the document into blocks
instead, and the accessors below *raise* on an absent or duplicated heading
rather than handing back an empty result -- a section the generator stopped
emitting has to fail a test, not quietly satisfy one.

CI installs no third-party packages for the unit suite (``coverage`` and
``ruff``, pinned in CONTRIBUTING.md), so this is stdlib-only. It handles only
what the generator can produce -- ATX headings, paragraphs, ``-`` bullet
lists, ordered lists, pipe tables, fenced code blocks and four-space literal
blocks -- and raises :class:`ReadmeError` on anything else. Being narrow is
the point: an unsupported construct is a failure, never a guess. In
particular it refuses

* a fence that is never closed;
* a table with no delimiter row, a delimiter row that is not all dashes, or a
  body row whose cell count disagrees with the header's;
* an ordered list that does not count up from 1;
* a line indented one to three spaces, which is neither a paragraph nor a
  four-space literal block;
* a block that starts on the line directly below another one, with no blank
  line between them -- CommonMark resolves several of those as lazy
  continuations of the block above, and guessing which is not this parser's
  job;
* a tab anywhere, since indentation is structural here;
* a line with trailing whitespace, because two trailing spaces are a hard
  line break in Markdown and this parser does not model breaks, so accepting
  one would misrepresent the document it was asked to describe.

Headings do not nest here: a section is the run of blocks between its own
heading and the next heading of *any* level. The generated README has no
subsections, and modelling a hierarchy it does not have would only let a
block land in the wrong section without the tests noticing.

Not collected as a test module (name doesn't start with ``test``), but
importable by files under tests/ once ``unittest discover -s tests`` puts this
directory on sys.path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_ORDERED_RE = re.compile(r"^(?P<number>\d+)\. (?P<text>.*)$")
_DELIMITER_CELL_RE = re.compile(r"^:?-+:?$")

# A literal block is indented by four spaces. Anything indented less is not a
# block of its own, and the parser says so rather than silently dedenting.
_LITERAL_INDENT = 4
# ATX headings stop at six '#'s; a seventh makes it a paragraph that happens
# to start with hashes, which this document never emits.
_MAX_HEADING_LEVEL = 6


class ReadmeError(ValueError):
    """Raised when the document is outside the supported subset or malformed."""


@dataclass(frozen=True)
class Block:
    """Common base: every block knows the 1-based line it starts on."""

    line: int


@dataclass(frozen=True)
class Heading(Block):
    level: int
    text: str


@dataclass(frozen=True)
class Paragraph(Block):
    lines: tuple[str, ...]

    @property
    def text(self) -> str:
        """The paragraph as one string, its soft line breaks joined by spaces."""
        return " ".join(self.lines)


@dataclass(frozen=True)
class BulletList(Block):
    items: tuple[str, ...]


@dataclass(frozen=True)
class OrderedList(Block):
    items: tuple[str, ...]


@dataclass(frozen=True)
class CodeBlock(Block):
    # The fence's info string: "bash" for ```bash, "" for a bare fence.
    info: str
    lines: tuple[str, ...]


@dataclass(frozen=True)
class LiteralBlock(Block):
    lines: tuple[str, ...]


@dataclass(frozen=True)
class Table(Block):
    header: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]

    @property
    def labels(self) -> tuple[str, ...]:
        """Each row's first cell, in order."""
        return tuple(row[0] for row in self.rows)

    def value(self, label: str, column: str | None = None) -> str:
        """Return the cell in row ``label``, under ``column``.

        ``column`` defaults to the second column, which is what a two-column
        settings table means by "the value". An absent or repeated label is an
        error: a row the generator stopped emitting must fail the test that
        reads it rather than come back empty.
        """
        matching = [row for row in self.rows if row[0] == label]
        if not matching:
            raise ReadmeError(f"table at line {self.line} has no row {label!r}; it has {list(self.labels)}")
        if len(matching) > 1:
            raise ReadmeError(f"table at line {self.line} has {len(matching)} rows labelled {label!r}")
        if column is None:
            if len(self.header) != 2:
                raise ReadmeError(
                    f"table at line {self.line} has {len(self.header)} columns, so value() needs a column name"
                )
            return matching[0][1]
        if self.header.count(column) != 1:
            raise ReadmeError(
                f"table at line {self.line} has {self.header.count(column)} columns named {column!r};"
                f" it has {list(self.header)}"
            )
        return matching[0][self.header.index(column)]


@dataclass(frozen=True)
class Section:
    """A heading and the blocks between it and the next heading."""

    heading: Heading
    blocks: tuple[Block, ...]

    @property
    def title(self) -> str:
        return self.heading.text

    def _only(self, kind: type, name: str) -> Block:
        """The section's one block of ``kind``; an error if none or several.

        Returning an empty result for an absent block is what made the old
        substring assertions weak, so this raises in both directions instead.
        """
        found = [block for block in self.blocks if type(block) is kind]
        if not found:
            raise ReadmeError(f"section {self.title!r} at line {self.heading.line} has no {name}")
        if len(found) > 1:
            raise ReadmeError(
                f"section {self.title!r} at line {self.heading.line} has {len(found)} {name}s, at lines "
                + ", ".join(str(block.line) for block in found)
            )
        return found[0]

    def paragraphs(self) -> tuple[Paragraph, ...]:
        return tuple(block for block in self.blocks if isinstance(block, Paragraph))

    def paragraph(self) -> Paragraph:
        """The section's one paragraph."""
        return self._only(Paragraph, "paragraph")

    def bullets(self) -> tuple[str, ...]:
        """The items of the section's one bullet list."""
        return self._only(BulletList, "bullet list").items

    def ordered(self) -> tuple[str, ...]:
        """The items of the section's one ordered list."""
        return self._only(OrderedList, "ordered list").items

    def code_blocks(self) -> tuple[CodeBlock, ...]:
        return tuple(block for block in self.blocks if isinstance(block, CodeBlock))

    def code_block(self) -> CodeBlock:
        """The section's one fenced block."""
        return self._only(CodeBlock, "code block")

    def literal_block(self) -> LiteralBlock:
        """The section's one four-space literal block."""
        return self._only(LiteralBlock, "literal block")

    def table(self) -> Table:
        """The section's one pipe table."""
        return self._only(Table, "table")


@dataclass(frozen=True)
class Document:
    blocks: tuple[Block, ...]

    @property
    def headings(self) -> tuple[Heading, ...]:
        return tuple(block for block in self.blocks if isinstance(block, Heading))

    def outline(self) -> tuple[tuple[int, str], ...]:
        """Every heading as ``(level, text)``, in document order.

        Asserted whole, this is what catches a heading that moved, one that
        was renamed, and a block emitted twice -- none of which a substring
        assertion can see.
        """
        return tuple((heading.level, heading.text) for heading in self.headings)

    def section(self, title: str) -> Section:
        """The section headed ``title``; an error if absent or duplicated."""
        positions = [
            index
            for index, block in enumerate(self.blocks)
            if isinstance(block, Heading) and block.text == title
        ]
        if not positions:
            raise ReadmeError(f"no heading {title!r}; the document has {[text for _, text in self.outline()]}")
        if len(positions) > 1:
            lines = [self.blocks[index].line for index in positions]
            raise ReadmeError(f"heading {title!r} appears {len(positions)} times, at lines {lines}")
        start = positions[0]
        end = start + 1
        while end < len(self.blocks) and not isinstance(self.blocks[end], Heading):
            end += 1
        return Section(self.blocks[start], tuple(self.blocks[start + 1 : end]))

    def bullets(self, title: str) -> tuple[str, ...]:
        return self.section(title).bullets()

    def code_blocks(self, title: str) -> tuple[CodeBlock, ...]:
        return self.section(title).code_blocks()

    def table(self, title: str) -> Table:
        return self.section(title).table()


def parse(text: str) -> Document:
    """Parse ``text`` into a :class:`Document`, in file order."""
    if "\t" in text:
        raise ReadmeError("tab character: indentation is structural here, so tabs are not supported")
    lines = text.split("\n")
    # A file ending in a newline splits to a final "" that is not a line.
    if lines and lines[-1] == "":
        lines.pop()
    for number, line in enumerate(lines, start=1):
        if line != line.rstrip():
            raise ReadmeError(f"trailing whitespace at line {number}: {line!r}")
    blocks: list[Block] = []
    index = 0
    while index < len(lines):
        if not lines[index]:
            index += 1
            continue
        block, index = _parse_block(lines, index)
        blocks.append(block)
    return Document(tuple(blocks))


def _kind(line: str) -> str:
    """Name the block a non-blank line opens, without consuming anything."""
    if line.startswith("#"):
        return "heading"
    if line.startswith("|"):
        return "table"
    if line.startswith("- "):
        return "bullet list"
    if _ORDERED_RE.match(line):
        return "ordered list"
    if line.startswith(" "):
        return "indented block"
    if line.startswith("```"):
        return "code block"
    return "paragraph"


def _parse_block(lines: list[str], index: int) -> tuple[Block, int]:
    kind = _kind(lines[index])
    if kind == "heading":
        return _parse_heading(lines, index)
    if kind == "code block":
        return _parse_code_block(lines, index)
    if kind == "table":
        return _parse_table(lines, index)
    if kind == "bullet list":
        return _parse_bullets(lines, index)
    if kind == "ordered list":
        return _parse_ordered(lines, index)
    if kind == "indented block":
        return _parse_literal(lines, index)
    return _parse_paragraph(lines, index)


def _require_break(lines: list[str], index: int, opened: str, start: int) -> None:
    """Refuse a block that begins on the line below another one.

    CommonMark resolves several of these as lazy continuations of the block
    above rather than as new blocks, so accepting them would mean choosing an
    interpretation. The generator always separates its blocks with a blank
    line, so this only ever fires on a generator that stopped doing that.
    """
    if index >= len(lines) or not lines[index]:
        return
    raise ReadmeError(
        f"{_kind(lines[index])} at line {index + 1} follows the {opened} opened at line {start + 1}"
        " with no blank line between them"
    )


def _parse_heading(lines: list[str], index: int) -> tuple[Heading, int]:
    line = lines[index]
    level = len(line) - len(line.lstrip("#"))
    if level > _MAX_HEADING_LEVEL:
        raise ReadmeError(f"heading at line {index + 1} has {level} '#'s, which is not a heading")
    rest = line[level:]
    if not rest.startswith(" "):
        raise ReadmeError(f"heading at line {index + 1} has no space after its '#'s: {line!r}")
    title = rest[1:]
    # Only one space separates the hashes from the text, and none trails it --
    # the trailing-whitespace rule above already refuses the empty case.
    if title != title.strip():
        raise ReadmeError(f"heading at line {index + 1} pads its text with spaces: {line!r}")
    if title.endswith("#"):
        raise ReadmeError(f"heading at line {index + 1} uses a closing '#' sequence, which this subset omits")
    return Heading(index + 1, level, title), index + 1


def _parse_code_block(lines: list[str], index: int) -> tuple[CodeBlock, int]:
    start = index
    info = lines[index][3:]
    if "`" in info:
        raise ReadmeError(f"code fence at line {index + 1} has a backtick in its info string: {lines[index]!r}")
    body: list[str] = []
    index += 1
    while True:
        if index >= len(lines):
            raise ReadmeError(f"code fence opened at line {start + 1} is never closed")
        if lines[index] == "```":
            index += 1
            break
        body.append(lines[index])
        index += 1
    block = CodeBlock(start + 1, info, tuple(body))
    _require_break(lines, index, "code block", start)
    return block, index


def _split_row(line: str, number: int) -> tuple[str, ...]:
    if len(line) < 2 or not line.endswith("|"):
        raise ReadmeError(f"table row at line {number} is not delimited by '|' at both ends: {line!r}")
    return tuple(cell.strip() for cell in line[1:-1].split("|"))


def _parse_table(lines: list[str], index: int) -> tuple[Table, int]:
    start = index
    header = _split_row(lines[index], index + 1)
    index += 1
    if index >= len(lines) or not lines[index].startswith("|"):
        raise ReadmeError(f"table at line {start + 1} has no delimiter row under its header")
    delimiter = _split_row(lines[index], index + 1)
    if len(delimiter) != len(header):
        raise ReadmeError(
            f"table at line {start + 1} has {len(header)} header cells but {len(delimiter)} delimiter cells"
        )
    for cell in delimiter:
        if not _DELIMITER_CELL_RE.match(cell):
            raise ReadmeError(f"table delimiter cell {cell!r} at line {index + 1} is not a run of dashes")
    index += 1
    rows: list[tuple[str, ...]] = []
    while index < len(lines) and lines[index].startswith("|"):
        row = _split_row(lines[index], index + 1)
        if len(row) != len(header):
            raise ReadmeError(f"table row at line {index + 1} has {len(row)} cells but the header has {len(header)}")
        rows.append(row)
        index += 1
    block = Table(start + 1, header, tuple(rows))
    _require_break(lines, index, "table", start)
    return block, index


def _parse_bullets(lines: list[str], index: int) -> tuple[BulletList, int]:
    start = index
    items: list[str] = []
    while index < len(lines) and lines[index].startswith("- "):
        items.append(lines[index][2:])
        index += 1
    block = BulletList(start + 1, tuple(items))
    _require_break(lines, index, "bullet list", start)
    return block, index


def _parse_ordered(lines: list[str], index: int) -> tuple[OrderedList, int]:
    start = index
    items: list[str] = []
    while index < len(lines):
        match = _ORDERED_RE.match(lines[index])
        if match is None:
            break
        number = int(match.group("number"))
        if number != len(items) + 1:
            raise ReadmeError(f"ordered list at line {start + 1} numbers its item {len(items) + 1} as {number}")
        items.append(match.group("text"))
        index += 1
    block = OrderedList(start + 1, tuple(items))
    _require_break(lines, index, "ordered list", start)
    return block, index


def _parse_literal(lines: list[str], index: int) -> tuple[LiteralBlock, int]:
    start = index
    body: list[str] = []
    while index < len(lines) and lines[index].startswith(" "):
        if not lines[index].startswith(" " * _LITERAL_INDENT):
            raise ReadmeError(
                f"line {index + 1} is indented {len(lines[index]) - len(lines[index].lstrip(' '))} spaces,"
                f" which is neither a paragraph nor a {_LITERAL_INDENT}-space literal block"
            )
        body.append(lines[index][_LITERAL_INDENT:])
        index += 1
    block = LiteralBlock(start + 1, tuple(body))
    _require_break(lines, index, "literal block", start)
    return block, index


def _parse_paragraph(lines: list[str], index: int) -> tuple[Paragraph, int]:
    start = index
    body: list[str] = []
    while index < len(lines) and lines[index]:
        if _kind(lines[index]) != "paragraph":
            raise ReadmeError(
                f"{_kind(lines[index])} at line {index + 1} follows the paragraph opened at line {start + 1}"
                " with no blank line between them"
            )
        body.append(lines[index])
        index += 1
    return Paragraph(start + 1, tuple(body)), index

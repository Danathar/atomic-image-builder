"""A strict parser for the block-YAML subset ``App.generate_recipe`` emits.

``generate_recipe`` builds the BlueBuild recipe by joining string literals --
deliberately, so the tool needs no PyYAML at runtime -- and the tests for it
asserted substring membership only. That leaves the shape of the document
unasserted: a package list emitted under ``remove:`` instead of ``install:``,
a service list under ``masked:`` instead of ``enabled:``, or an indentation
slip that makes the file unparseable all keep every ``assertIn`` passing.

CI installs no third-party packages for the unit suite (``coverage`` and
``ruff``, pinned in CONTRIBUTING.md), so this parses the document instead of
importing yaml. It handles only what the recipe generator can produce --
block mappings, block sequences, plain and double-quoted scalars, and ``|``
literal blocks -- and raises :class:`BlockYamlError` on anything else,
including the inconsistent indentation, duplicate keys, and flow collections
a real parser would reject or silently reinterpret. Being narrow is the
point: an unsupported construct is a failure, never a guess.

Not collected as a test module (name doesn't start with ``test``), but
importable by files under tests/ once ``unittest discover -s tests`` puts
this directory on sys.path.
"""

from __future__ import annotations

import re

# YAML treats ":" as a key separator only when a space or a line end follows
# it, which is why "ghcr.io/ublue-os/brew:latest" stays a scalar. Mirror that
# rule rather than splitting on the first colon.
_KEY_RE = re.compile(r"^(?P<key>[^\s:][^:]*):(?:[ ](?P<value>.*))?$")


class BlockYamlError(ValueError):
    """Raised when the document is outside the supported subset or malformed."""


def parse(text: str) -> object:
    """Parse ``text`` into dicts, lists and strings."""
    lines = [
        line.rstrip()
        for line in text.splitlines()
        if line.strip() and line.strip() != "---" and not line.lstrip().startswith("#")
    ]
    if not lines:
        return None
    value, index = _parse_node(lines, 0, _indent(lines[0]))
    if index != len(lines):
        raise BlockYamlError(f"trailing content at line {index + 1}: {lines[index]!r}")
    return value


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _parse_node(lines: list[str], index: int, indent: int) -> tuple[object, int]:
    if lines[index].strip() == "-" or lines[index].strip().startswith("- "):
        return _parse_sequence(lines, index, indent)
    return _parse_mapping(lines, index, indent)


def _parse_mapping(lines: list[str], index: int, indent: int) -> tuple[dict[str, object], int]:
    mapping: dict[str, object] = {}
    while index < len(lines):
        current = _indent(lines[index])
        if current < indent:
            break
        if current > indent:
            raise BlockYamlError(f"unexpected indent at line {index + 1}: {lines[index]!r}")
        body = lines[index].strip()
        if body.startswith("-"):
            break
        match = _KEY_RE.match(body)
        if match is None:
            raise BlockYamlError(f"not a mapping entry at line {index + 1}: {body!r}")
        key = match.group("key").strip()
        if key in mapping:
            raise BlockYamlError(f"duplicate key {key!r} at line {index + 1}")
        raw_value = (match.group("value") or "").strip()
        index += 1
        mapping[key], index = _parse_value(lines, index, indent, raw_value)
    return mapping, index


def _parse_sequence(lines: list[str], index: int, indent: int) -> tuple[list[object], int]:
    items: list[object] = []
    while index < len(lines):
        current = _indent(lines[index])
        if current < indent:
            break
        if current > indent:
            raise BlockYamlError(f"unexpected indent at line {index + 1}: {lines[index]!r}")
        body = lines[index].strip()
        if not body.startswith("-"):
            break
        if body != "-" and not body.startswith("- "):
            raise BlockYamlError(f"malformed sequence entry at line {index + 1}: {body!r}")
        rest = body[2:] if body.startswith("- ") else ""
        # An entry's content starts two columns in, under the "- ", and any
        # continuation of it is a block at that indent.
        content_indent = indent + 2
        match = _KEY_RE.match(rest)
        if match is not None:
            # "- type: dnf" opens a mapping whose first key is on this line.
            lines[index] = " " * content_indent + rest
            item, index = _parse_mapping(lines, index, content_indent)
            items.append(item)
            continue
        item, index = _parse_value(lines, index + 1, indent, rest)
        items.append(item)
    return items, index


def _parse_value(lines: list[str], index: int, indent: int, raw: str) -> tuple[object, int]:
    """Resolve a scalar written inline, a ``|`` block, or a nested block below."""
    if raw == "|":
        return _parse_literal_block(lines, index, indent)
    if raw:
        return _scalar(raw), index
    if index < len(lines) and _indent(lines[index]) > indent:
        return _parse_node(lines, index, _indent(lines[index]))
    if index < len(lines) and _indent(lines[index]) == indent and lines[index].strip().startswith("-"):
        # A sequence may sit at its key's own indent; YAML allows both.
        return _parse_sequence(lines, index, indent)
    return None, index


def _parse_literal_block(lines: list[str], index: int, indent: int) -> tuple[str, int]:
    block: list[str] = []
    block_indent: int | None = None
    while index < len(lines) and _indent(lines[index]) > indent:
        if block_indent is None:
            block_indent = _indent(lines[index])
        elif _indent(lines[index]) < block_indent:
            raise BlockYamlError(f"under-indented literal line at line {index + 1}: {lines[index]!r}")
        block.append(lines[index][block_indent:])
        index += 1
    if block_indent is None:
        raise BlockYamlError(f"empty literal block at line {index}")
    return "\n".join(block), index


def _scalar(raw: str) -> str:
    if raw[:1] in {"[", "{", "&", "*", "!"}:
        raise BlockYamlError(f"unsupported YAML construct: {raw!r}")
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        inner = raw[1:-1]
        if '"' in inner.replace('\\"', ""):
            raise BlockYamlError(f"unbalanced quoting: {raw!r}")
        return inner.replace('\\"', '"')
    if '"' in raw or raw.endswith(":"):
        raise BlockYamlError(f"scalar needs quoting: {raw!r}")
    return raw

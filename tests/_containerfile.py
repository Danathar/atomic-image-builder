"""A strict parser for the Containerfile subset the generators emit.

``generate_containerfile`` builds the Containerfile by joining string literals,
and the tests for it asserted substring membership only -- and only about the
Homebrew block. That left the rest of the document unasserted: the build could
lose ``RUN bootc container lint``, invoke ``/ctx/nope.sh`` instead of
``/ctx/build.sh``, drop the ``COPY build_files /`` that fills the ``ctx``
stage, or bind-mount that stage somewhere the command never looks, and every
``assertIn``/``assertNotIn`` in the suite still passed. None of those are
cosmetic: each one produces a Containerfile that builds a different image, or
no image at all.

Substrings cannot see any of it because the meaning lives in the structure --
which stage a ``COPY`` draws ``--from``, which mount a ``RUN`` gets, what the
command is once the backslash continuations are joined, and what order the
instructions run in. So this parses the file into instructions instead.

CI installs no third-party packages for the unit suite (``coverage`` and
``ruff``, pinned in CONTRIBUTING.md), so this is stdlib-only. It handles only
what the generators can produce -- comments, blank lines, ``FROM``, ``COPY``
and ``RUN``, each optionally carrying ``--key=value`` flags, with backslash
line continuations -- and raises :class:`ContainerfileError` on anything else,
including a continuation that runs off the end of the file and an instruction
with no argument. Being narrow is the point: an unsupported construct is a
failure, never a guess.

Not collected as a test module (name doesn't start with ``test``), but
importable by files under tests/ once ``unittest discover -s tests`` puts this
directory on sys.path.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Every instruction the generators emit. A Containerfile keyword outside this
# set is a construct these tests have no assertions for, so it is an error
# rather than an opaque instruction that quietly passes through.
SUPPORTED = frozenset({"FROM", "COPY", "RUN"})


class ContainerfileError(ValueError):
    """Raised when the file is outside the supported subset or malformed."""


@dataclass(frozen=True)
class Instruction:
    """One logical instruction, with its continuation lines already joined."""

    keyword: str
    flags: tuple[str, ...]
    argument: str
    # 1-based line number of the instruction's first physical line, so a
    # failure names the place in the generated file rather than the index.
    line: int = 0

    def flag(self, name: str) -> str:
        """Return the value of ``--name=value``, or raise if it is absent."""
        prefix = f"--{name}="
        for item in self.flags:
            if item.startswith(prefix):
                return item[len(prefix) :]
        raise ContainerfileError(f"{self.keyword} at line {self.line} has no --{name}")

    def mounts(self) -> tuple[dict[str, str], ...]:
        """Return each ``--mount=`` flag parsed into its comma-separated fields."""
        parsed: list[dict[str, str]] = []
        for item in self.flags:
            if not item.startswith("--mount="):
                continue
            fields: dict[str, str] = {}
            for part in item[len("--mount=") :].split(","):
                key, sep, value = part.partition("=")
                if not sep or not key:
                    raise ContainerfileError(f"malformed mount field {part!r} at line {self.line}")
                fields[key] = value
            parsed.append(fields)
        return tuple(parsed)


@dataclass(frozen=True)
class From(Instruction):
    """``FROM <image>`` or ``FROM <image> AS <stage>``."""

    image: str = ""
    stage: str | None = None


@dataclass(frozen=True)
class Copy(Instruction):
    """``COPY [--from=<stage>] <source>... <destination>``."""

    sources: tuple[str, ...] = field(default_factory=tuple)
    destination: str = ""


def parse(text: str) -> list[Instruction]:
    """Parse ``text`` into a list of instructions, in file order."""
    physical = text.splitlines()
    instructions: list[Instruction] = []
    index = 0
    while index < len(physical):
        stripped = physical[index].strip()
        if not stripped or stripped.startswith("#"):
            index += 1
            continue
        start = index
        parts: list[str] = []
        # A trailing backslash continues the instruction onto the next line.
        while True:
            if index >= len(physical):
                raise ContainerfileError(
                    f"continuation runs off the end of the file, from line {start + 1}"
                )
            current = physical[index].rstrip()
            index += 1
            if current.endswith("\\"):
                parts.append(current[:-1].strip())
                continue
            parts.append(current.strip())
            break
        instructions.append(_build(" ".join(part for part in parts if part), start + 1))
    return instructions


def _build(logical: str, line: int) -> Instruction:
    keyword, _, rest = logical.partition(" ")
    keyword = keyword.upper()
    if keyword not in SUPPORTED:
        raise ContainerfileError(f"unsupported instruction {keyword!r} at line {line}")
    flags: list[str] = []
    tokens = rest.split()
    # Flags precede the argument; the first non-flag token starts the argument,
    # so a "--" spelling later in a shell command is never read as a flag.
    while tokens and tokens[0].startswith("--"):
        flag = tokens.pop(0)
        if "=" not in flag:
            raise ContainerfileError(f"flag {flag!r} at line {line} has no value")
        flags.append(flag)
    argument = " ".join(tokens)
    if not argument:
        raise ContainerfileError(f"{keyword} at line {line} has no argument")
    if keyword == "FROM":
        return _build_from(tuple(flags), argument, line)
    if keyword == "COPY":
        return _build_copy(tuple(flags), argument, line)
    return Instruction(keyword, tuple(flags), argument, line)


def _build_from(flags: tuple[str, ...], argument: str, line: int) -> From:
    tokens = argument.split()
    if len(tokens) == 1:
        return From("FROM", flags, argument, line, image=tokens[0], stage=None)
    if len(tokens) == 3 and tokens[1].upper() == "AS":
        return From("FROM", flags, argument, line, image=tokens[0], stage=tokens[2])
    raise ContainerfileError(f"malformed FROM at line {line}: {argument!r}")


def _build_copy(flags: tuple[str, ...], argument: str, line: int) -> Copy:
    tokens = argument.split()
    if len(tokens) < 2:
        raise ContainerfileError(f"COPY at line {line} needs a source and a destination")
    return Copy(
        "COPY",
        flags,
        argument,
        line,
        sources=tuple(tokens[:-1]),
        destination=tokens[-1],
    )

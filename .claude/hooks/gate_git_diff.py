#!/usr/bin/env python3
"""Refuse the `git` arguments that reach a file the index does not hold.

`.claude/settings.json` allows `Bash(git diff:*)` and `Bash(git log:*)`
outright, and denies `Read(./cosign.key)`, `Read(./.env)`, `Read(**/*.pem)`
and the two SSH key patterns. Those two statements are only consistent while
`git` cannot open a path the way the Read tool would, and by default it can:

* `git diff --no-index <a> <b>` diffs two arbitrary paths on the filesystem,
  printing both whole when one of them is `/dev/null`.
* `git diff --output=<path>` and `git log --output=<path>` create or truncate
  an arbitrary file, which makes "reads" the wrong word for the command.
* `-O<path>` names an order file, a third path outside the index, and
  arrives as `-aO<path>` as readily as on its own: git bundles short
  options into one word.

A `Read(...)` rule gates the Read tool and never sees a path that arrives as
an argument to Bash, and every form above matches the allowed prefix, so none
of them raises a prompt. This hook is what makes the deny rules true of the
whole tool surface rather than of one tool.

It runs on `PreToolUse` for `Bash` and exits 2 -- the blocking code, whose
stderr goes back to the model as the reason -- when a `git` invocation in the
command carries one of those arguments. Anything else exits 0 and is left
alone, so `git diff`, `git diff -- path`, `git log -p` and range arguments
such as `HEAD..main` are unaffected.

Operands are judged lexically: absolute, or carrying a `..` path component.
Resolving them with `realpath` and comparing against the checkout root reads
as the stricter test and is not one, because it folds `..` away before
comparing -- a sibling checkout resolves to an allowed prefix while still
naming a denied file.
"""

from __future__ import annotations

import json
import shlex
import sys

# Long options that reach outside the index, by the name git knows them by.
# Matched against unambiguous-prefix abbreviations too: git resolves any
# unambiguous prefix of an option, and refusing the ambiguous ones as well
# costs nothing -- git rejects those itself.
REFUSED_LONG = ("no-index", "output", "ext-diff")

# Short options that take a path, by letter. `O` is the order file.
REFUSED_SHORT = frozenset("O")

# Short options of `git diff` and `git log` that take a value. git bundles
# single-letter options into one word and reads the value of the first
# value-taking letter from whatever follows it in that word, so `-aOorder`
# is `-a -O order` while `-SOrder` is `-S Order`. This set is where the walk
# in refused_short() stops reading letters as options. A letter missing from
# it is walked past as a boolean, which can only over-refuse -- the value
# of an unlisted option gets read as more options -- and never lets an `O`
# through; a letter wrongly listed here would.
VALUED_SHORT = frozenset("BCGILMOSUXln")

# Options git takes *before* a subcommand, which move the repository it acts
# on or inject configuration into it. `-c diff.external=<command>` is the
# shortest path from a permitted `git diff` to running an arbitrary program,
# and `--git-dir` points the whole command at another checkout. Position
# matters: `git log -c` and `git diff -C` are ordinary diff options and are
# left alone, so these are only refused ahead of the subcommand.
REFUSED_GLOBAL = (
    "-c",
    "-C",
    "--config-env",
    "--exec-path",
    "--git-dir",
    "--work-tree",
    "--namespace",
)

# Environment variables with the same reach, set as a prefix to the command.
# `GIT_EXTERNAL_DIFF` names a program git runs on every file it diffs; the
# `GIT_CONFIG*` family and `GIT_DIR` re-point what the command reads.
REFUSED_ENVIRONMENT = (
    "GIT_EXTERNAL_DIFF",
    "GIT_CONFIG",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
    "GIT_CONFIG_COUNT",
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)

# Shell operators that end one command and begin another. The scan follows
# them so a `git` call after `&&`, in a pipeline or inside `$(...)` is read as
# a command rather than as arguments to the first one.
OPERATORS = frozenset({"&&", "||", ";", "|", "&", "(", ")", "{", "}", "\n"})

VERB_PREFIXES = ("$(", "(", "`", "<(", ">(")


def refused_long(token: str) -> bool:
    """Is this token a long option that reaches outside the index?

    `--` is the operand separator, not an option, and its empty name would
    otherwise prefix-match everything.
    """
    if not token.startswith("--") or token == "--":
        return False
    name = token[2:].split("=", 1)[0]
    if not name:
        return False
    return any(name == refused or refused.startswith(name) for refused in REFUSED_LONG)


def refused_short(token: str) -> bool:
    """Does this token carry a refused short option, alone or in a cluster?

    Only the letters up to and including the first value-taking one are
    options; the rest of the word is that option's value. Matching the start
    of the token alone would pass `-aOorder`, which git reads exactly as
    `-O order` with `-a` in front.
    """
    if not token.startswith("-") or token.startswith("--"):
        return False
    for letter in token[1:]:
        if letter in REFUSED_SHORT:
            return True
        if letter in VALUED_SHORT:
            return False
    return False


def unsafe_operand(token: str) -> bool:
    """A path that leaves the checkout, spelled without needing `--no-index`.

    `git diff a b` run outside a repository implies `--no-index`, so an
    operand is the second way to name a file the index does not hold. A `..`
    *path component* is the test: `HEAD..main` is a revision range and carries
    no `/`, so it is left alone.
    """
    if token.startswith("-"):
        return False
    if token.startswith("/") or token.startswith("~"):
        return True
    return ".." in token.split("/")


def tokenize(command: str) -> list[str]:
    """The command's words and shell operators, or a failure.

    `punctuation_chars` is what makes `&&` and `|` arrive as tokens of their
    own instead of being glued to the word beside them.
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    return list(lexer)


def segments(tokens: list[str]) -> list[list[str]]:
    """The token list split into commands on shell operators."""
    found: list[list[str]] = [[]]
    for token in tokens:
        if token in OPERATORS or set(token) <= {"&", "|", ";"} and token:
            found.append([])
            continue
        found[-1].append(token)
    return [segment for segment in found if segment]


def bare(token: str) -> str:
    """A token with substitution punctuation stripped from its front, so the
    `git` in `$(git diff ...)` is read as the command it is."""
    for prefix in VERB_PREFIXES:
        while token.startswith(prefix):
            token = token[len(prefix) :]
    return token


def assignment(token: str) -> str | None:
    """The variable name in a `NAME=value` prefix, or None."""
    name = bare(token).split("=", 1)[0]
    return name if "=" in bare(token) and name.isidentifier() else None


def split_segment(segment: list[str]) -> tuple[list[str], str, list[str]]:
    """A segment as (environment names, command, arguments)."""
    names: list[str] = []
    for index, token in enumerate(segment):
        name = assignment(token)
        if name is not None:
            names.append(name)
            continue
        return names, bare(token).rsplit("/", 1)[-1], segment[index + 1 :]
    return names, "", []


def global_refusal(arguments: list[str]) -> str | None:
    """The refused option, if one appears before the subcommand.

    Only the options ahead of the subcommand are read here: `-c` and `-C`
    after it are diff options with no such reach.
    """
    for token in arguments:
        if not token.startswith("-"):
            return None
        if token.split("=", 1)[0] in REFUSED_GLOBAL:
            return token
    return None


def refusal(command: str) -> str | None:
    """Why this command is blocked, or None when it is left alone."""
    try:
        tokens = tokenize(command)
    except ValueError:
        # Unbalanced quoting. What the shell would do with it cannot be read
        # here, so it is refused rather than guessed at.
        return "the command cannot be parsed as shell words, so its git arguments cannot be checked"
    for segment in segments(tokens):
        names, command_name, arguments = split_segment(segment)
        if command_name != "git":
            continue
        for name in names:
            if name in REFUSED_ENVIRONMENT:
                return (
                    f"{name} changes what git runs or which repository it reads, "
                    "so the command is no longer the read the allow list describes"
                )
        option = global_refusal(arguments)
        if option is not None:
            return (
                f"{option} rewrites git's configuration or its repository before the "
                "subcommand runs, which reaches past what the allow list describes"
            )
        for token in arguments:
            if refused_long(token) or refused_short(token):
                return (
                    f"{token} lets git read or write a file the index does not hold, "
                    "which the Read(./cosign.key), Read(./.env) and Read(**/*.pem) deny "
                    "rules in .claude/settings.json exist to prevent"
                )
            if unsafe_operand(token):
                return (
                    f"{token} names a path outside the checkout; git diff implies "
                    "--no-index for operands like it, which reads any file on disk"
                )
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError):
        print("gate_git_diff: hook input is not JSON", file=sys.stderr)
        return 2
    if payload.get("tool_name") != "Bash":
        return 0
    command = payload.get("tool_input", {}).get("command", "")
    if not isinstance(command, str):
        print("gate_git_diff: Bash command is not a string", file=sys.stderr)
        return 2
    reason = refusal(command)
    if reason is None:
        return 0
    print(f"Refused: {reason}.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())

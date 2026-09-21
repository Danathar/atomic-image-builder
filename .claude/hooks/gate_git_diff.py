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
* `git diff <(true) <path>` is `--no-index` again: bash replaces the
  process substitution with a `/dev/fd/N` path, which is outside the
  checkout, and git prints `<path>` whole beside it.
* `git diff HEAD >cosign.pub` is `--output` in the shell's own spelling: bash
  opens the target for writing before git starts, so the file is truncated
  whatever git then prints, and `>>`, `>|`, `&>`, `2>err`, `>&file` and
  `<>file` each open a path the same way. Bash also lets the redirection
  precede the command name, so `>cosign.pub git diff HEAD` is the same
  command -- and shlex hands that `>` back as the first token of the
  segment, where a scan that took the first token for the command name saw
  no `git` at all and checked nothing else in the segment either.
* `git diff $(echo /dev/null) ./cosign.key` is `--no-index` once more: bash
  rebuilds the word before git runs, and the gate reads words as typed. A
  `$VAR`, a `${VAR}`, a `$(...)` or `` `...` `` substitution and a `$'...'`
  escape (`--outpu$'\x74'=cosign.pub` is `--output=cosign.pub`) each spell
  an argument this hook cannot see, so a word carrying an unquoted `$` or
  backtick is refused; a single-quoted one (`--format='%h $x'`) is a literal
  to bash and passes.

A `Read(...)` rule gates the Read tool and never sees a path that arrives as
an argument to Bash, and every form above matches the allowed prefix, so none
of them raises a prompt. This hook is what makes the deny rules true of the
whole tool surface rather than of one tool.

The redirection is not git's alone. Thirteen other allow rows in
`.claude/settings.json` carry a trailing `:*` -- "this command with any
arguments" -- and an output redirection is part of the string that rule
matches, so `shellcheck contrib/aib >cosign.pub` truncated the trust anchor
before a line was linted (bash opens the target first, so the file is
emptied even when the command then fails) and `hadolint Containerfile
>.claude/settings.json` overwrote the file holding these rules, both with no
prompt. Those commands are named in GATED_PREFIXES, and an output
redirection inside any of them is refused the way one inside a git
invocation is; descriptor forms, input redirections, pipes and a command no
allow rule covers are left alone. The rows with no `:*` (`ruff check`,
`actionlint`, the exact test commands) need no entry: a redirection makes
the string match none of them and Claude Code prompts.

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
import re
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

# The allow rows in `.claude/settings.json` that carry a trailing `:*`, other
# than git's, which refusal() covers above: each is a command prefix the
# permission layer approves with any arguments after it, and a shell output
# redirection is part of "any arguments". An output redirection inside one of
# these commands is refused for the reason it is refused inside a git one --
# the shell opens the target before the command runs. The rows with no `:*`
# (`ruff check`, `actionlint`, `python3 -m unittest discover -s tests`, ...)
# are absent on purpose: a redirection makes the string match none of them and
# Claude Code prompts. tests/test_git_diff_gate.py derives this list from the
# settings file rather than restating it, so a rule added there fails until it
# is listed here. None of these commands takes a flag that names a file to
# write (shellcheck and hadolint report to stdout, `just --fmt --check` only
# checks, `maintenance_audit.py` has no output option), so the redirection is
# the whole of the write primitive on this list.
GATED_PREFIXES = (
    ("shellcheck",),
    ("hadolint",),
    ("python3", "maintenance_audit.py", "--skip-upstream"),
    ("just", "--fmt", "--check"),
    ("skopeo", "inspect"),
    ("podman", "ps"),
    ("podman", "logs"),
    ("podman", "inspect"),
    ("podman", "images"),
    ("podman", "image", "exists"),
    ("gh", "label", "list"),
    ("gh", "search", "issues"),
    ("gh", "search", "prs"),
)

# Shell words that stand before the name of the command they run, which a
# leading-words match has to step over the way it steps over an assignment:
# `time shellcheck x >out` and `command shellcheck x >out` are shellcheck's
# redirection. A wrapper's own options are not modelled, so `env -i shellcheck`
# is not matched; it matches no allow rule either, and prompts on its own.
COMMAND_WRAPPERS = frozenset(
    {"time", "command", "builtin", "exec", "env", "nohup", "nice"}
)


VERB_PREFIXES = ("$(", "(", "`", "<(", ">(")

# Process substitution. bash replaces `<(command)` and `>(command)` with a
# `/dev/fd/N` path before git runs, so `git diff <(true) ./cosign.key` hands
# git an operand outside the checkout that neither starts with `/` nor
# carries a `..`, and git diff implies --no-index and prints the key beside
# it. shlex breaks the word at the `(`, emitting `<(` as a token of its own,
# and the `)` that closes it is read as the end of the command, so an operand
# after the substitution (`git diff <(true) /etc/passwd`) is not even in the
# segment unsafe_operand() reads. A word that opens either way is refused in
# any git invocation; the segment it lands in is the one being checked.
PROCESS_SUBSTITUTION = ("<(", ">(")

# The characters that make bash rebuild a word before git runs: `$` opens a
# parameter (`$VAR`, `${VAR}`), a command substitution (`$(...)`) or an
# ANSI-C escape (`$'\x74'`), and a backtick opens the older substitution.
# The gate reads words as typed and cannot see what any of them becomes, and
# each can spell a refused argument out of pieces that are not refused --
# `$(echo /dev/null)`, `--outpu$'\x74'=`. A word carrying either is refused
# where bash would read it: unquoted, or inside double quotes, which quote
# neither. Single quotes and a backslash do, so `--format='%h $x'` passes.
# See expands_at_runtime().
EXPANSION = frozenset("$`")

# The shape of every brace expansion bash performs: a `{`, then a `,` or a
# `..` somewhere after it, then a `}` somewhere after that. See
# brace_would_expand().
EXPANDING_BRACE = re.compile(r"\{.*(?:,|\.\.).*\}", re.DOTALL)

# A redirection operator as shlex hands it back: `punctuation_chars` glues a
# run of `<>&|` into one token, so `>`, `>>`, `>|`, `&>`, `&>>`, `>&`, `<`,
# `<<`, `<<<`, `<&` and `<>` each arrive whole, and the descriptor of `2>err`
# arrives as the token `2` before it. `|` and `&&` carry neither angle
# bracket and stay the separators they are; `<(` and `>(` carry a `(` and
# are the process substitutions handled above. See redirection_writes_a_path().
REDIRECTION = re.compile(r"^[<>&|]*[<>][<>&|]*$")

# The descriptor bash reads off the front of a redirection when the token
# before the operator has this shape: the digits of `2>err`, or the `{name}`
# of `{fd}>file`, which allocates a descriptor into a variable. shlex splits
# either off as a token of its own.
DESCRIPTOR = re.compile(r"^(?:[0-9]+|\{[A-Za-z_][A-Za-z0-9_]*\})$")

# A run of shell punctuation as shlex hands it back, and the pieces bash
# reads it as. `punctuation_chars` glues every adjacent `();<>|&` into one
# token, so `echo x;(git diff)` arrives with `;(` as a word and `x=$(git
# diff);echo` with `);`. Neither is a separator to is_operator() nor a `(`
# or `)` to segments(), so the first hid its git from the scan altogether
# and the second ran the outer command into the inner one. Each paren is
# a token of its own to bash, except the `<(` and `>(` of a process
# substitution, which stay whole so the prefix test above still sees them;
# what is left between parens (`;`, `&&`, `>`) is the operator it was.
# See punctuation_pieces().
PUNCTUATION = frozenset("();<>|&")


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


def brace_would_expand(token: str) -> bool:
    """Does bash brace-expand this token before git ever sees it?

    shlex does no brace expansion, so `--outpu{t,t}=/tmp/x` reaches this hook
    as one word while bash hands git two: `--output=/tmp/x --output=/tmp/x`.
    Every refused spelling can be reassembled this way -- `--no-inde{x,x}`,
    `-aO{,}order`, `{/etc/passwd,x}` -- so the word the gate reads matches none
    of the tests above while the words git runs do. Such a word is refused
    rather than expanded: modelling bash's expansion in full (nesting,
    `{1..9}` sequences, quoting) is where the next hole hides.

    Not every brace, though. Bash expands a brace only when a comma or a `..`
    range sits inside it, and leaves any other brace as a literal -- which is
    what git's own `@{...}` revision syntax relies on: `HEAD@{1}`,
    `main@{upstream}`, `@{-1}`, `@{2.days.ago}`. Those reach git as typed and
    touch none of the arguments this hook refuses, so refusing them blocked the
    ordinary diff against the previous commit for nothing. A `{` that never
    closes is a literal to bash as well, and passes.

    The test is deliberately cruder than bash's: a `{`, then a `,` or a `..`
    anywhere after it, then a `}` anywhere after that. No nesting or matching
    is tracked. A depth counter that closed a brace at the first `}` missed
    the comma in `{--src-prefix=x},--no-index}`, which bash expands to
    `--src-prefix=x}` and `--no-index` because it pairs the `{` with the
    *last* `}` it can; every refinement toward bash's real rule is a chance
    to disagree with it in some other direction. Over-refusing is the safe
    direction: `HEAD@{2}..HEAD@{1}` is refused too, though bash would leave
    it alone, and the refusal says to write `HEAD~2..HEAD~1`. `${VAR}` is
    refused as a runtime-built argument this hook cannot inspect; the `$`
    in it is refused by expands_at_runtime() as well, the same answer by
    another route.

    The token is bash's word with the quote marks removed, which is what
    shlex hands back. Removing quotes never removes a brace, a comma or a
    dot, so a quoted comma (`{a",",b}`) or a quoted operator (`{a';',b}`) --
    both of which bash still expands -- cannot hide the shape; a fully
    quoted `"{a,b}"`, which bash leaves alone, is refused as the price.
    """
    return "${" in token or EXPANDING_BRACE.search(token) is not None


def redirection_writes_a_path(operator: str, target: str) -> bool:
    """Does this redirection make the shell open `target` for writing?

    Every operator with a `>` in it does -- `>`, `>>`, `>|`, `&>`, `&>>`,
    and `<>`, which opens read-write and creates the file -- and so does
    `>&` when its target is a path (`>&file` is bash's older `&>file`). The
    exception is a target that names a descriptor: `>&1`, `2>&1` and `>&-`
    duplicate or close a descriptor and touch no path. `<`, `<<`, `<<<` and
    `<&` open nothing for writing. `2>&file` is an ambiguous redirect to bash
    and writes nothing, and is refused anyway: the rule is the operator and
    the target's shape, not a model of bash's error paths.
    """
    if ">" not in operator:
        return False
    if operator.endswith("&") and (target.isdigit() or target == "-"):
        return False
    return True


def expands_at_runtime(twin: str) -> bool:
    """Does bash rebuild this word before git sees it?

    `twin` is the word's masked copy from mask_quotes(): every character
    bash takes literally is a `Q`, and a `$` or a backtick survives only
    where bash would act on it. So `$G`, `$(...)`, `` `...` `` and the `$`
    of `$'\\x74'` are all found, while `'%h $x'`, `\\$x` and `"\\$x"` are
    not. The token itself is no use here: shlex hands back the same `$x`
    for `'$x'` and `$x`.

    A `$` that bash leaves alone -- one that ends the word, or sits before
    a character that opens nothing -- is refused with the rest; the gate
    reads words as typed, and the price of that is naming the literal
    through single quotes.
    """
    return any(char in twin for char in EXPANSION)


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


def mask_quotes(command: str) -> str:
    """The command with every quoted or escaped character replaced by `Q`.

    shlex removes the quotes as it splits, so the `;` of `git diff ';' >out`
    arrives as the same token as the `;` of `git diff; >out`, and a split
    that read it as a separator put the redirection in a segment with no
    git in it while bash handed git a literal `;` and truncated the file
    (review on #414). The masked copy keeps every quoted region the same
    length and free of shell syntax, so splitting it with the same lexer
    gives one token per real token, and a token that is a separator in
    the masked copy is one bash would honour; a quoted one is not.

    Double quotes are the exception for two characters: bash still opens a
    substitution at a `$` or a backtick inside them, so those two are kept
    where a double quote encloses them and expands_at_runtime() can read
    the twin for what bash would rebuild. Neither is a separator or a
    quote to the lexer, so the token count is unchanged; a `(` after the
    `$` is still masked, so `"$(x)"` opens no nested segment. An escaped
    `\\$` is a literal inside double quotes as out of them, and is masked.
    """
    masked: list[str] = []
    quote = ""
    escaped = False
    for char in command:
        if escaped:
            escaped = False
            masked.append("Q")
        elif quote:
            if char == quote:
                quote = ""
                masked.append("Q")
            elif quote == '"' and char == "\\":
                escaped = True
                masked.append("Q")
            elif quote == '"' and char in EXPANSION:
                masked.append(char)
            else:
                masked.append("Q")
        elif char == "\\":
            escaped = True
            masked.append("Q")
        elif char in "'\"":
            quote = char
            masked.append("Q")
        else:
            masked.append(char)
    return "".join(masked)


def strip_comments(command: str) -> str:
    """The command with every shell comment removed.

    A `#` that begins a word after whitespace (or the start of the string)
    starts a comment bash drops through the end of the line, so `shellcheck
    contrib/aib # output > file` opens nothing and must not be refused for
    the `>` in the comment (review on aurora-zfs-simple#211, the bash twin
    of this hook). The spans are found on the quote-masked copy, where a
    quoted `#` is a `Q`, and cut from the command itself; tokenize() is then
    handed a string with no comment in it, and its `commenters = ""` still
    holds for the `#` this leaves in place: one inside a word (`HEAD^#x`),
    which is a character of the word, and one straight after an operator
    (`;#`), which is kept as a word and can only over-refuse.
    """
    masked = mask_quotes(command)
    kept: list[str] = []
    index = 0
    while index < len(command):
        if masked[index] == "#" and (index == 0 or masked[index - 1] in " \t\n"):
            end = masked.find("\n", index)
            index = len(command) if end == -1 else end
            continue
        kept.append(command[index])
        index += 1
    return "".join(kept)


def tokenize(command: str) -> list[str]:
    """The command's words and shell operators, or a failure.

    `punctuation_chars` is what makes `&&` and `|` arrive as tokens of their
    own instead of being glued to the word beside them.

    `commenters` is emptied because shlex treats `#` as a comment character by
    default, even in the middle of a word, and drops everything after it -- but
    bash only starts a comment at a `#` that begins a word. So `git diff HEAD^#x
    /etc/passwd` reaches bash as three operands (`--no-index` is implied and it
    reads `/etc/passwd`), while a lexer with the default comment character sees
    only `git diff HEAD^` and lets the command through. The gate has to read the
    same words bash runs, so the comment character is turned off.

    A run of punctuation is split back into the pieces bash reads, so a
    `(` or `)` glued to a separator (`;(`, `);`, `&&(`) is the paren and the
    separator rather than a word of its own. See punctuation_pieces().
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    tokens: list[str] = []
    for token in lexer:
        if len(token) > 1 and set(token) <= PUNCTUATION:
            tokens.extend(punctuation_pieces(token))
        else:
            tokens.append(token)
    return tokens


def punctuation_pieces(run: str) -> list[str]:
    """A glued run of shell punctuation as the tokens bash reads it as.

    Each paren is a token of its own, except that a `<` or `>` right before
    a `(` is the opening of a process substitution and stays with it, so
    `;<(` is `;` and `<(`. Whatever lies between parens is kept whole: it is
    the separator or redirection it was, and is_operator() and REDIRECTION
    read it as before.
    """
    pieces: list[str] = []
    for piece in re.split(r"([()])", run):
        if not piece:
            continue
        if piece == "(" and pieces and pieces[-1][-1] in "<>":
            piece = pieces[-1][-1] + piece
            pieces[-1] = pieces[-1][:-1]
            if not pieces[-1]:
                pieces.pop()
        pieces.append(piece)
    return pieces


def is_operator(token: str) -> bool:
    return token in OPERATORS or bool(token) and set(token) <= {"&", "|", ";"}


def segments(tokens: list[str], masked: list[str]) -> list[tuple[list[str], list[str]]]:
    """The token list split into commands on the shell operators bash honours.

    `masked` is the same list lexed from mask_quotes(); a token is a
    separator only when its masked twin is, so a quoted `;` or `|` stays a
    word of its command. A `$(...)` is a nested command: its words become a
    segment of their own, and the command around it goes on after the `)`
    with the `$` still in place, so `>$(printf cosign.pub) git diff HEAD`
    is one command whose redirection names a target built at runtime, and
    a split that ended the command at the `(` had put that redirection in a
    segment with no git in it (review on #414). A `<(...)` or `>(...)` is
    nested the same way, with its opening token kept in the outer command.

    Each segment is returned as (words, twins): the tokens of the command
    and their masked copies in the same order, so a check that needs to
    know what bash would quote -- expands_at_runtime() -- can read the twin
    of the word it is judging.
    """
    found: list[tuple[list[str], list[str]]] = [([], [])]
    outer: list[tuple[list[str], list[str]]] = []
    for token, twin in zip(tokens, masked, strict=True):
        if twin == "(" and found[-1][0] and found[-1][0][-1].endswith("$"):
            outer.append(found.pop())
            found.append(([], []))
            continue
        if twin in PROCESS_SUBSTITUTION:
            # A process substitution is a nested command too, and the `<(`
            # stays in the outer command as the operand it becomes, so
            # `shellcheck <(printf x) >cosign.pub` is one shellcheck command
            # whose redirection is its own (review on #420), and the `<(`
            # refusal for a git word still sees its token.
            found[-1][0].append(token)
            found[-1][1].append(twin)
            outer.append(found.pop())
            found.append(([], []))
            continue
        if twin == ")" and outer:
            found.append(outer.pop())
            continue
        if is_operator(twin):
            found.append(([], []))
            continue
        found[-1][0].append(token)
        found[-1][1].append(twin)
    return [segment for segment in found if segment[0]]


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


def after_redirection(segment: list[str], index: int) -> int:
    """The index just past the redirection whose operator is at `index`.

    A redirection is its operator and the token after it, the target. A
    target that opens a backtick substitution runs to the token that closes
    it, since shlex splits the substitution's words apart: `` >`printf
    cosign.pub` shellcheck contrib/aib `` still finds its name at shellcheck
    (review on #420, for split_segment() before it).
    """
    index += 1  # the operator
    if index < len(segment):
        target = segment[index]
        index += 1  # the target
        if target.startswith("`") and not (len(target) > 1 and target.endswith("`")):
            while index < len(segment) and not segment[index - 1].endswith("`"):
                index += 1
    return index


def split_segment(segment: list[str]) -> tuple[list[str], str, list[str]]:
    """A segment as (environment names, command, arguments).

    The command is the first token that is neither an assignment nor part of
    a redirection. Bash lets a redirection precede the command name, so in
    `>cosign.pub git diff HEAD` the first token is `>` and the command is
    still git; reading `>` as the name left the whole segment unchecked. A
    redirection is its operator, the token after it (the target), and a
    descriptor right before it (the `2` of `2>err`, the `{fd}` of
    `{fd}>file`). shlex does not say whether the descriptor touched the
    operator, so `2 >err git log` is read the same way; that names a command
    called `2` as git, which can only over-refuse. A target that opens a
    backtick substitution runs to the token that closes it, since shlex
    splits the substitution's words apart: `` >`printf x` git diff `` still
    finds its name at git.
    """
    names: list[str] = []
    index = 0
    while index < len(segment):
        token = segment[index]
        name = assignment(token)
        if name is not None:
            names.append(name)
            index += 1
            continue
        if REDIRECTION.match(token):
            index = after_redirection(segment, index)
            continue
        if (
            DESCRIPTOR.match(token)
            and index + 1 < len(segment)
            and REDIRECTION.match(segment[index + 1])
        ):
            index += 1  # the descriptor; the operator is next
            continue
        return names, bare(token).rsplit("/", 1)[-1], segment[index + 1 :]
    return names, "", []


def command_words(segment: list[str]) -> tuple[list[str], bool]:
    """The words of a segment that the command receives, in order.

    A redirection -- its operator, its target and a descriptor written before
    it -- is the shell's, not the command's, and bash lets it stand anywhere
    in the simple command, so `>cosign.pub shellcheck x` and `shellcheck
    >cosign.pub x` both come back as `['shellcheck', 'x']`. A leading
    assignment and a leading wrapper word (`time`, `command`, `env`) are
    stepped over too, since neither is part of the prefix an allow rule
    matches. The first word left is the command's name, with a leading path
    stripped the way split_segment() strips it. The second value says
    whether a wrapper was stepped over: its own options come before the name
    it runs (`command -p shellcheck x`), so gated_prefix() then looks for the
    prefix at every later word rather than only the first.
    """
    words: list[str] = []
    wrapped = False
    index = 0
    while index < len(segment):
        token = segment[index]
        if REDIRECTION.match(token):
            index = after_redirection(segment, index)
            continue
        if (
            DESCRIPTOR.match(token)
            and index + 1 < len(segment)
            and REDIRECTION.match(segment[index + 1])
        ):
            index += 1  # the descriptor; the operator is next
            continue
        if token.startswith(PROCESS_SUBSTITUTION):
            index += 1  # the substitution's opening; its body is a segment of its own
            continue
        if not words and (
            assignment(token) is not None or bare(token) in COMMAND_WRAPPERS
        ):
            wrapped = wrapped or bare(token) in COMMAND_WRAPPERS
            index += 1
            continue
        words.append(bare(token).rsplit("/", 1)[-1] if not words else token)
        index += 1
    return words, wrapped


def gated_prefix(segment: list[str]) -> tuple[str, ...] | None:
    """The GATED_PREFIXES entry this segment's leading words match, or None.

    Behind a wrapper the name may stand after the wrapper's own options
    (`command -p shellcheck x >cosign.pub`), so every later word is tried as
    the start; without one, only the first word names the command.
    """
    words, wrapped = command_words(segment)
    starts = range(len(words)) if wrapped else range(min(len(words), 1))
    for start in starts:
        candidates = [bare(words[start]).rsplit("/", 1)[-1], *words[start + 1 :]]
        for prefix in GATED_PREFIXES:
            if tuple(candidates[: len(prefix)]) == prefix:
                return prefix
    return None


def writing_redirection(segment: list[str]) -> str | None:
    """The first redirection in `segment` that opens a path for writing,
    spelled as typed with its descriptor, or None."""
    for index, token in enumerate(segment):
        if not REDIRECTION.match(token):
            continue
        target = segment[index + 1] if index + 1 < len(segment) else ""
        if not redirection_writes_a_path(token, target):
            continue
        descriptor = (
            segment[index - 1]
            if index > 0 and DESCRIPTOR.match(segment[index - 1])
            else ""
        )
        return f"{descriptor}{token}{target}"
    return None


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
        command = strip_comments(command)
        tokens = tokenize(command)
        masked = tokenize(mask_quotes(command))
    except ValueError:
        # Unbalanced quoting. What the shell would do with it cannot be read
        # here, so it is refused rather than guessed at.
        return "the command cannot be parsed as shell words, so its git arguments cannot be checked"
    if len(tokens) != len(masked):
        # The masked copy split differently, so which tokens are separators
        # cannot be told; refused rather than guessed at.
        return "the command's quoting cannot be matched to its words, so its git arguments cannot be checked"
    for segment, twins in segments(tokens, masked):
        names, command_name, arguments = split_segment(segment)
        if command_name != "git":
            prefix = gated_prefix(segment)
            if prefix and (
                any(token.startswith(PROCESS_SUBSTITUTION) for token in segment)
                or any(expands_at_runtime(twin) for twin in twins)
            ):
                return (
                    f"a substitution -- $(...), a backtick, <(...) or >(...) -- in "
                    f"`{' '.join(prefix)}` runs the command inside it as part of a string "
                    "the allow rule approved on its prefix alone, and that inner command "
                    "is held to no rule: `podman images >(cat >cosign.pub)` and "
                    "`shellcheck $(>cosign.pub)` truncate the file while the command "
                    "prints as usual; write the inner command as a command of its own"
                )
            redirection = writing_redirection(segment) if prefix else None
            if prefix and redirection:
                return (
                    f"{redirection} makes the shell open a file for writing before "
                    f"`{' '.join(prefix)}` runs, which truncates it whatever the command "
                    "then prints; the allow rule for that command matches its prefix and "
                    "the redirection is the rest of the string, so nothing else would "
                    "prompt -- it is the same write this hook refuses for `git diff HEAD "
                    ">cosign.pub`. These commands print to stdout, so read that or pipe it "
                    "(2>&1, >&2, an input redirection, and a redirection on a command no "
                    "allow rule covers are not refused)"
                )
            continue
        for token in segment:
            if token.startswith(PROCESS_SUBSTITUTION):
                return (
                    f"{token} opens a process substitution, which bash replaces with a "
                    "/dev/fd path before git runs; that names a file outside the "
                    "checkout without spelling --no-index or an absolute path, and the ) "
                    "that closes it hides every later operand from this check"
                )
            if brace_would_expand(token):
                return (
                    f"{token} carries a brace that bash expands before git runs, and "
                    "the expansion can spell --output, --no-index, -O or a path outside "
                    "the checkout that none of the other tests see in the word as typed; "
                    "a brace with no comma and no .. after it before a }, such as "
                    "HEAD@{1}, is a literal to bash and is not refused, while a .. "
                    "between two reflog entries (HEAD@{2}..HEAD@{1}) is refused with "
                    "the rest, so write HEAD~2..HEAD~1 instead"
                )
        for index, token in enumerate(segment):
            if not REDIRECTION.match(token):
                continue
            target = segment[index + 1] if index + 1 < len(segment) else ""
            descriptor = (
                segment[index - 1]
                if index > 0 and DESCRIPTOR.match(segment[index - 1])
                else ""
            )
            if redirection_writes_a_path(token, target):
                return (
                    f"{descriptor}{token}{target} makes the shell open {target or 'its target'} for "
                    "writing before git runs, which truncates the file whatever git then "
                    "prints -- the same write --output makes, in the shell's own spelling, "
                    "and one bash accepts before the command name as readily as after it; "
                    "git diff and git log print to stdout, so read that instead (2>&1, "
                    ">&2, an input redirection, and a redirection on another command of "
                    "the same string are not refused)"
                )
        for token, twin in zip(segment, twins, strict=True):
            if expands_at_runtime(twin):
                return (
                    f"{token} carries a $ or a backtick that bash acts on, so the word "
                    "git runs is built at runtime -- from a variable, a $(...) or "
                    "backtick substitution, or a $'...' escape -- and the gate reads "
                    "words as typed; bash would rebuild them, and the rebuilt word can "
                    "spell --output, --no-index, -O or a path outside the checkout that "
                    "no test here sees; write the argument out literally (a "
                    "single-quoted literal such as --format='%h $x' is not refused, and "
                    "neither is a $ on another command of the same string)"
                )
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

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

Nor is the *read* git's alone. `Bash(shellcheck:*)` is allowed outright too,
and ShellCheck prints the **source line** above every diagnostic it reports,
so pointing it at a denied file prints that file back: `shellcheck ./.env`
prints every unexported `NAME=value` line, values included, and `shellcheck
./cosign.key` prints the key's `-----BEGIN-----` line and its base64 body,
because a line of base64 ending in `=` is an assignment to ShellCheck and
each one earns an `SC2034` with the line above it. It is a lossy `cat`, and
for the shapes the deny rules name the loss is nothing that matters. No
permission pattern closes it, because those match by prefix:
`Bash(shellcheck tests/*)` still matches `shellcheck tests/x.sh
/home/me/.aws/credentials`. So a shellcheck invocation gets an operand scan
of its own (see shellcheck_refusal()): every operand must stay inside the
checkout and must not carry one of the `Read(...)` deny shapes. A glob is
expanded here and each file it names is checked, which is what keeps this
repository's own lint command -- it ends in `tests/e2e/*.sh` -- working; a
brace or an unquoted leading `~` is refused instead, since `{x,.env}` is two
words to bash and `~` is `$HOME`, and neither is a spelling any lint run
here needs.

Not every operand arrives in the argv, either. `SHELLCHECK_OPTS` is not a
list of options despite the name: ShellCheck splits it and prepends it to its
own argument list, operands included, so `SHELLCHECK_OPTS=./.env shellcheck
contrib/aib` lints the `.env` as well and prints its lines back while the
argv the operand scan reads names only `contrib/aib`. It is one of a family:
`GIT_EXTERNAL_DIFF` names a program git runs on every file it diffs,
`GIT_CONFIG_*` and `GIT_DIR` re-point what git reads, `PYTHONPATH` puts a
module of its own ahead of the audit's imports, and `LD_PRELOAD` loads code
into any of them. Each stands *before* the command name, so a refusal scoped
to the invocation it feeds catches only the leading spelling, and bash has
eight others: `VAR+=x cmd` (appending to an unset variable creates it),
`env VAR=x cmd`, `env -i`, `env 'VAR'=x`, `env -S'VAR=x cmd'`,
`--split-string=`, `export VAR=x; cmd`, and `declare -x` / `typeset -x` in a
segment of its own with no gated command in it at all. So a word assigning
one of those names is refused wherever it stands and whatever value it
carries -- nothing in this repository sets any of them, which is what lets
the rule be unconditional rather than a judgement about which values are
harmless. Unconditional also closes what scoping it to "a string that also
runs a gated command" cannot: the Bash tool's shell outlives one call, so an
`export` approved on its own would still be in the environment of the next
call's `git diff`. See REFUSED_ENVIRONMENT and assigned_environment().

The name a command is spelled with is the last of that family. A path
(`/usr/bin/git`), a wrapper (`env`, `command`, `nice`, `timeout`, `noglob`)
and a brace (`{,git} diff` -- bash drops the empty word and runs git) all
reach the same tool while spelling the name differently, and a scan that
read only the first word of the segment found no gated command in any of
them and checked nothing else. command_words() steps over the shell's own
words to the name, and gated_prefix() and git_arguments() both read the
result, so the git half and the redirection half of this hook see the same
command.

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

Three limits of the shellcheck scan, stated rather than implied. A glob is
expanded against the directory this hook runs in, which is the session's
working directory rather than the checkout root, and against the files that
exist when it runs; a file created between the check and the command is the
one case where the words bash builds are not the words checked here. And a
`shellcheck -x` run whose target names an outside file in a `source`
directive reads that file on the operands' behalf: the operands are checked,
what the tool then opens for them is not. `-x` is load-bearing in this
repository's own lint command, so it is not refused. And the environment
rule reads the word rather than the command it belongs to, so a word that
merely quotes the assignment is refused with one that makes it: search for
a variable by its name alone (`grep -n SHELLCHECK_OPTS docs/SECURITY-AI.md`)
rather than with the `=` attached.

Four shapes of the same corpus (#428) are decided the other way, and are
written here rather than left undecided:

* The pager. `GIT_PAGER=prog git log` runs nothing: git spawns a pager only
  when stdout is a terminal, and a command run by the Bash tool has a pipe,
  which is also why `PAGER=cat git log` stays unprompted. Held by a reach
  test rather than by this paragraph, so a git that changed it would fail.
* `GIT_SSH_COMMAND`, `GIT_ASKPASS` and `GIT_EDITOR` name programs git runs
  for a connection or an edit. `git diff`, `git log` and `git status` are the
  whole of the allow list and make neither, so no rule here reaches them.
* `PYTHONSTARTUP` is read by an interactive interpreter only, and
  `PYTEST_ADDOPTS` by a pytest this repository's rules never start: the
  `python3` rows are `python3 -m unittest discover -s tests` and the
  `--skip-upstream` audit.
* A glob in a git word is expanded by bash rather than refused here, because
  a glob cannot name a file outside the working directory without a `/`, a
  `..` or a `~` in the pattern, and unsafe_operand() refuses each of those in
  the pattern as readily as in a plain operand. The shellcheck scan expands
  its own globs because there the *result* can be a denied shape inside the
  checkout; `git diff` reads nothing it is not already allowed to read.

One wrapper is refused rather than stepped over. `xargs` adds words it
reads from standard input, or from the file its `-a` names, to the command
it runs, so `xargs git diff <list.txt`, with `/dev/null` and `./cosign.key`
the two lines of that file, hands git the two operands `--no-index` needs
while this string names neither of them -- and Claude Code's matcher reads
`xargs git diff` as `git diff`, so the allow rule approves it and nothing
prompts. No scan of this string can check words that are not in it, so an
`xargs` that runs git or a GATED_PREFIXES command is refused wherever it
stands among the wrappers (see xargs_fed_command()). One in front of a
command no rule covers (`xargs echo`, `xargs grep -n git`) is left alone:
Claude Code prompts for that on its own.

And one residual, which a `PreToolUse` hook reading one command string cannot
close: a wrapper that takes its command from somewhere this hook cannot read
-- `sh -c '...'`, `find -exec` -- reaches any tool. Neither matches an allow
rule, so Claude Code prompts for them on its own; that prompt, not this hook,
is what covers them.
"""

from __future__ import annotations

import fnmatch
import glob
import json
import re
import shlex
import sys
from typing import NamedTuple

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
# `--upload-pack` and `--receive-pack` name a program to run at the other end
# of a connection. git takes neither before a subcommand today -- they are
# options of `fetch`, `clone` and `push`, none of which any allow rule covers
# -- so they are listed against a git that starts accepting them there rather
# than for a hole that is open now (#428).
REFUSED_GLOBAL = (
    "-c",
    "-C",
    "--config-env",
    "--exec-path",
    "--git-dir",
    "--work-tree",
    "--namespace",
    "--upload-pack",
    "--receive-pack",
)

# Environment variables with the same reach, and what each one reaches. A
# variable is one row: the name, as an fnmatch pattern so a numbered family
# (`GIT_CONFIG_KEY_0`) is one entry, and the clause the refusal is built from.
# Every row is refused wherever the word assigning it stands -- see
# assigned_environment() for the spellings -- so the list is names that reach
# a command `.claude/settings.json` allows, not names that merely look
# dangerous; the four decided the other way are in the module docstring.
REFUSED_ENVIRONMENT = (
    (
        "GIT_EXTERNAL_DIFF",
        "names a program git runs on every file it diffs",
    ),
    (
        "GIT_CONFIG*",
        "injects configuration into git before the subcommand runs, which is "
        "what `-c diff.external=` does in git's own spelling",
    ),
    (
        "GIT_DIR",
        "re-points the repository the command reads",
    ),
    (
        "GIT_WORK_TREE",
        "re-points the tree the command compares against",
    ),
    (
        "GIT_INDEX_FILE",
        "re-points the index the command diffs against",
    ),
    (
        "GIT_NAMESPACE",
        "re-points the refs the command resolves",
    ),
    (
        "GIT_OBJECT_DIRECTORY",
        "re-points the object store the command reads",
    ),
    (
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "adds an object store the command reads",
    ),
    (
        "SHELLCHECK_OPTS",
        "is not a list of options despite the name -- shellcheck splits it and "
        "prepends it to its own argument list, operands included, so "
        "SHELLCHECK_OPTS=./.env in front of a lint run lints the .env as well "
        "and prints its lines back, past the Read(./cosign.key), Read(./.env) "
        "and Read(**/*.pem) deny rules in .claude/settings.json and with no "
        "such path in the argv the operand scan reads",
    ),
    (
        "PYTHONPATH",
        "puts a module of its own ahead of the audit's imports",
    ),
    (
        "PYTHONHOME",
        "re-points the standard library the audit imports from",
    ),
    (
        "LD_PRELOAD",
        "loads a library of its own into every one of these commands before a "
        "line is linted or a diff is printed",
    ),
    (
        "LD_AUDIT",
        "loads a library of its own into the dynamic linker of every one of "
        "these commands",
    ),
    (
        "LD_LIBRARY_PATH",
        "re-points the shared libraries every one of these commands loads",
    ),
    (
        "GH_HOST",
        "sends the token gh authenticates with to another host",
    ),
    (
        "GH_ENTERPRISE_TOKEN",
        "replaces the token gh authenticates with",
    ),
    (
        "CONTAINERS_CONF",
        "re-points podman's and skopeo's configuration, runtime included",
    ),
    (
        "CONTAINERS_REGISTRIES_CONF",
        "re-points the registries podman and skopeo resolve an image against",
    ),
    (
        "CONTAINERS_STORAGE_CONF",
        "re-points the image store podman reads",
    ),
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
# redirection, and `env git diff --no-index a b` is git's operand. A wrapper's
# own options are not modelled as taking values, so the name is looked for at
# every word after one (`command -p shellcheck x`, `timeout 5 shellcheck x`),
# which can only over-refuse. `noglob` is zsh's: bash has no such command and
# fails, but only after it has opened the redirection, and zsh runs the
# command, so `noglob podman ps >out` truncates `out` in either shell, and
# Claude Code steps over it before matching an allow rule as it does `nohup`.
# A wrapper that takes its command from somewhere else -- `sh -c`, `find
# -exec` -- is absent on purpose: the command it runs is not a word of this
# string, and neither matches an allow rule, so Claude Code prompts for them
# on its own. `xargs` is absent for the opposite reason: its command is a
# word of this string, but the operands it hands that command are not, and
# an allow rule matches `xargs git diff` as readily as `git diff`. Stepping
# over it would check the operands that are written and pass the ones that
# are read from standard input, so it is refused instead; see
# xargs_fed_command().
COMMAND_WRAPPERS = frozenset(
    {
        "time",
        "command",
        "builtin",
        "exec",
        "env",
        "nohup",
        "nice",
        "timeout",
        "stdbuf",
        "setsid",
        "noglob",
    }
)

# xargs's own options, which stand between `xargs` and the command it runs,
# as GNU findutils parses them (`xargs --help`; tests/test_git_diff_gate.py
# holds the split against the real xargs). A short option that takes a value
# reads it from the rest of its word, or from the next word when nothing
# follows the letter, so `-n1 git` and `-n 1 git` both run git. The letters
# listed here take no value (`-0`, `-r`) or take one only in the same word
# (`-i{}`, `-l1`, `-eEOF`), which is why `-i git diff` runs git. A letter in
# neither set is read both ways -- the next word as its value, and as the
# command -- so an option this table does not know can only over-refuse.
# A long option with `=value` attached reads nothing more; alone, one that
# abbreviates only XARGS_PLAIN_LONG names reads nothing more either, and any
# other (`--max-a 1`, `--arg-file list.txt`, one xargs does not have) is read
# both ways too. See xargs_command_starts().
XARGS_FLAG_SHORT = frozenset("0oprtx")
XARGS_ATTACHED_SHORT = frozenset("eil")
XARGS_VALUED_LONG = (
    "--arg-file",
    "--delimiter",
    "--max-args",
    "--max-chars",
    "--max-procs",
    "--process-slot-var",
)
XARGS_PLAIN_LONG = (
    "--null",
    "--open-tty",
    "--interactive",
    "--no-run-if-empty",
    "--verbose",
    "--exit",
    "--show-limits",
    "--eof",
    "--replace",
    "--max-lines",
    "--help",
    "--version",
)

# The builtins whose own arguments assign, the same family
# assigned_environment() already reads every word of, by name --
# `export`, `declare -x` and `typeset -x` put a variable in every later
# command's environment; a bare `declare` or `readonly` does not export,
# but is refused with the rest all the same, the safe over-refusing
# direction assigned_environment()'s own docstring gives. Used by
# opaque_assignment_prefix() to scope a check assigned_environment() and
# runtime_assignment() cannot make by content alone: see there.
EXPORT_FAMILY = frozenset({"export", "declare", "typeset", "readonly"})

# ShellCheck's options that take their value as the *next* word, in both
# spellings. The attached forms (`-sbash`, `--shell=bash`) need no entry:
# each is one dash-prefixed word and the operand scan steps over it with the
# other options. `--rcfile` is absent on purpose -- it only accepts the
# attached `--rcfile=FILE` form -- and so is `-C`, whose argument is optional
# and must be attached, so `shellcheck -C always` is the flag plus a file
# named `always`, which is how ShellCheck reads it too.
SHELLCHECK_VALUE_OPTIONS = frozenset(
    {
        "-i",
        "-e",
        "-f",
        "-o",
        "-P",
        "-s",
        "-S",
        "-W",
        "--include",
        "--exclude",
        "--format",
        "--enable",
        "--source-path",
        "--shell",
        "--severity",
        "--wiki-link-count",
    }
)

# GNU env's own options this scan has to recognise in every spelling env
# itself accepts: getopt clustering for the short forms (`-iC/etc` is `-i`
# then `-C/etc` sharing one word; `-iS'...'` the same for -S) and
# unambiguous prefix abbreviation for the long forms (`--chd=/tmp/other`,
# `--split='...'`). `-C, --chdir=DIR` relocates the child before it runs;
# `-S, --split-string=S` re-splits one word into a command line of its
# own. Both reach past what an allow rule matches the same way
# GIT_EXTERNAL_DIFF does: `env -C /tmp/other git diff` reaches the
# allow-listed `git diff` and prints another repository's unstaged
# content, and `env -S'SHELLCHECK_OPTS=./.env shellcheck x'` re-splits
# a word into an assignment and a gated command word.split() never sees
# as two words. See wrapper_relocates(), env_split_string_value() and
# env_split_string_detached().
#
# `-u, --unset=NAME` is GNU env's third value-taking option; it reaches
# nothing this scan refuses on its own, but still has to end a short
# cluster's scan the way `-C` and `-S` do -- `-iuFOO -C /etc` would
# otherwise walk past its attached value and misread the `C` inside it
# for env's own -C.
ENV_VALUED_SHORT = frozenset("CSu")
WRAPPER_RELOCATION_LONG = "--chdir"
WRAPPER_SPLIT_STRING_LONG = "--split-string"


# Both of bash's assignment operators, longest first. `+=` appends, and
# appending to a variable that is not set creates it, so `SHELLCHECK_OPTS+=x`
# reaches the command's environment exactly as `=` does (review on #425).
ASSIGNMENT_OPERATORS = ("+=", "=")

# The file shapes `.claude/settings.json` denies the Read tool, as basename
# patterns. Staying inside the checkout is not enough on its own: `cosign.key`
# and a `.env` live there, and they are the files those rules exist for. The
# match is on the basename wherever the file sits, which is wider than
# `Read(./cosign.key)`; a `cosign.key` one directory down is the same secret.
# tests/test_git_diff_gate.py derives this list from the settings file, so a
# `Read(...)` rule added there fails until it is listed here.
DENIED_READ_SHAPES = (
    "cosign.key",
    ".env",
    ".env.*",
    "*.pem",
    "id_rsa",
    "id_ed25519",
)

# The characters that make bash expand a word into filenames. Unlike a brace
# or a `~`, a glob is expanded here rather than refused: this repository's own
# lint command ends in `tests/e2e/*.sh`, and refusing it would mean the check
# CONTRIBUTING.md and the pull-request template both name could not be run.
# Python's glob reads `*`, `?` and `[...]` the way bash does with its default
# options -- no `dotglob`, no `globstar`, no `extglob` -- so the files it
# names are the words bash would hand ShellCheck.
GLOB = frozenset("*?[")


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


RUNTIME_ASSIGNMENT_NAME = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\+?\Z")


def prefix_reaches_refused_environment(prefix: str) -> bool:
    """Could `prefix` -- the known, unexpanded start of a variable name --
    still grow into one of REFUSED_ENVIRONMENT's names?

    runtime_assignment() reads a name only up to the point bash would
    rebuild it further, so `prefix` is not necessarily the whole name --
    what follows the live `$` or backtick could still be more identifier
    characters ahead of the `=`. Both directions of startswith() are
    tested against each pattern's own prefix (its trailing `*` stripped,
    for GIT_CONFIG*'s sake): `prefix` may be a name walked only part of
    the way towards a pattern (`GIT_EXTE`, headed for
    `GIT_EXTERNAL_DIFF`), or already longer than a pattern's fixed stem
    (`GIT_CONFIG_KEY_0`, past `GIT_CONFIG`'s own `*`).

    Without this, runtime_assignment() refused every identifier this
    shape could ever put in front of a live `$` or backtick, whatever it
    was -- `gh release create v$TAG` carries none of the primitives this
    hook exists to catch, and `v` is not the start of any name in
    REFUSED_ENVIRONMENT, but the word was refused all the same, since the
    scan never asked whether the name it could not fully read was one of
    the ones that matter.
    """
    name = prefix.rstrip("+")
    for pattern, _ in REFUSED_ENVIRONMENT:
        stem = pattern.rstrip("*")
        if name.startswith(stem) or stem.startswith(name):
            return True
    return False


def runtime_assignment(token: str, twin: str) -> bool:
    """Does this word concatenate a plain variable name onto a live `$` or
    backtick that bash expands before assigned_environment() ever reads it,
    where the name it plausibly continues into is one REFUSED_ENVIRONMENT
    covers?

    `export GIT_EXTERNAL_DIFF$'=./evil'` is one word to bash -- an
    unquoted bareword and an ANSI-C-quoted string concatenate with nothing
    between them -- and it assigns exactly what `export
    GIT_EXTERNAL_DIFF=./evil` does once the quote expands. Read as typed,
    the token still carries a literal `=` (shlex only strips the quote
    marks, and single quotes pass their contents through unchanged), but
    it sits right after the `$`, so assigned_name()'s partition on the
    first `=` returns `GIT_EXTERNAL_DIFF$` -- a name carrying a live `$`
    that matches no REFUSED_ENVIRONMENT pattern.

    The test is the word up to its first live expansion character (found
    in `twin`, built by mask_quotes_stripped() so its indices line up
    with `token`'s own: a `$` or backtick that survives it is one bash
    acts on): if that prefix alone is already a plain bash identifier --
    `GIT_EXTERNAL_DIFF`, optionally with a trailing `+` -- *and* it could
    still grow into one of REFUSED_ENVIRONMENT's names
    (prefix_reaches_refused_environment()), the word plausibly continues
    into a variable name this scan cannot read past the expansion, and it
    is refused rather than partitioned on whatever `=` happens to follow.
    `x=$(git log -1)` does not match: its `$` sits after the `=` this scan
    already read `x` off of, not before it, so the assigned name is the
    literal `x` regardless of what the substitution's value becomes.
    `--outpu$'\\x74'=cosign.pub` does not match either: `--outpu` is not a
    bash identifier, so this is left to the git-argument scan that already
    refuses it by a different route. `gh release create v$TAG` does not
    match a third way: `v` is a bash identifier, but reaches no name this
    scan has to assume dangerous, and is left alone.
    """
    for index, char in enumerate(token):
        if char in EXPANSION and index < len(twin) and twin[index] == char:
            prefix = token[:index]
            return bool(
                RUNTIME_ASSIGNMENT_NAME.match(prefix)
                and prefix_reaches_refused_environment(prefix)
            )
    return False


def opaque_assignment_prefix(word: str, twin: str) -> bool:
    """Does this word of an export-family command open with a live `$` or
    backtick, so nothing about the name it assigns can be read at all?

    runtime_assignment() and prefix_reaches_refused_environment() decide a
    live expansion by testing the *known* prefix ahead of it against
    REFUSED_ENVIRONMENT's own names, and an empty prefix trivially
    satisfies every one of them (every name starts with the empty
    string) -- refusing on content alone here would refuse `curl
    "$URL=1"` exactly as it refuses `export $'GIT_EXTERNAL_DIFF=./evil'`,
    since both words are, as far as their own characters go, a live `$`
    with an `=` somewhere after it and nothing else to go on. The two are
    only told apart by where the word stands: `curl`'s argument is not
    read as an assignment by anything, while every word after
    export/declare/typeset/readonly's own name is -- assigned_environment()
    already tests all of them by content, and this is the one shape
    content cannot decide, so the caller passes only those words in (see
    its use in refusal()).

    `export $'GIT_EXTERNAL_DIFF=./evil'` is one word to bash, an ANSI-C
    quote with nothing before the `$` to concatenate onto, and it exports
    exactly what `export GIT_EXTERNAL_DIFF=./evil` does; unlike a bare
    leading assignment (`NAME=value cmd`), which bash's parser only
    recognises when the word carries no quote or expansion character at
    all -- confirmed against bash directly, `$'GIT_FOO=bar' env` and
    `'GIT_FOO=bar' env` both fail as "command not found" -- export and its
    family read their own arguments as NAME=value only *after* every
    expansion has already run, so a live character at the very front
    reaches a name this scan can never read, however it turns out.
    """
    return bool(word[:1] and word[0] in EXPANSION and twin[:1] == word[:1])


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


def mask_quotes_stripped(command: str) -> str:
    """Like mask_quotes(), but with the quote mark itself and a real
    escaping backslash dropped rather than masked, so tokenizing the result
    lines up character for character with tokenize(command)'s own tokens.

    mask_quotes() keeps every quoted region the same *length* as the
    original command -- that is what strip_comments() needs, to index the
    masked and the real copy together -- and gets it by masking the quote
    mark to a `Q` rather than dropping it the way shlex's own quote removal
    does. A membership test never notices the difference (expands_at_runtime()
    only asks whether a live `$` or backtick is anywhere in the twin), but a
    test that walks token and twin at the *same index* does: runtime_assignment()
    matches a plain identifier against the live `$` or backtick that follows
    it, and a twin padded with an extra `Q` per quote mark drifts out of
    alignment with the real token from the first quoted character on.
    `'GIT_EXTERNAL_DIFF'$'=./evil'` is bash's `GIT_EXTERNAL_DIFF=./evil` once
    the quotes are gone -- the `$` sits at index 17 of the 25-character real
    token -- but mask_quotes()'s twin puts it at index 19, behind the two
    `Q`s standing in for the quote marks mask_quotes() keeps and shlex does
    not, so the character at the real `$`'s index reads as `Q` and the word
    passed as clear of every REFUSED_ENVIRONMENT name.

    Two shapes need more than dropping the mark to line up right:

    An empty pair (`''`, `""`) loses every character of itself either way,
    and when it is the *whole word* -- `<<<''`'s here-string target -- that
    costs something rather than fixing it: nothing is left in the masked
    copy to mark that a word stood there, so tokenizing it returns one
    *fewer* token than tokenize(command) does and the two cannot be zipped
    together. A quote character immediately followed by its own match, at
    the start and the end of a word (the character before it is absent,
    whitespace or punctuation, and so is the character after its close),
    gets one placeholder `Q` instead of zero characters -- nothing walks
    past index 0 of an empty real token, so its length never has to match
    the placeholder's. Elsewhere in a word an empty pair contributes
    nothing, matching shlex exactly: `'GIT_EXTERNAL_DIFF'''$'=./evil'` is
    still `GIT_EXTERNAL_DIFF=./evil` at both the real and the masked
    length, since the embedded `''` is not what holds the word together.

    A backslash inside double quotes is only shlex's escape character in
    front of the quote mark or another backslash (posix shlex's
    `escapedquotes` is `"` alone); in front of anything else -- `$`, a
    backtick, an ordinary letter -- it is kept as a literal character of
    the word and the character after it is read normally, exactly as
    `"a\\zb"` reaches shlex as the four characters `a\\zb` rather than
    three. mask_quotes() does not need this distinction: either reading
    costs the same one `Q` per character against the original command's
    own length. This one does, since dropping a backslash shlex keeps is
    the same one-character skew a dropped quote mark caused.
    """
    masked: list[str] = []
    quote = ""
    escaped = False
    index = 0
    length = len(command)
    boundary = " \t\n" + "".join(PUNCTUATION)
    while index < length:
        char = command[index]
        if escaped:
            escaped = False
            masked.append("Q")
        elif quote:
            if char == quote:
                quote = ""
            elif quote == '"' and char == "\\" and index + 1 < length and command[index + 1] in ('"', "\\"):
                # shlex's own escapedquotes rule: it drops this backslash
                # and keeps only the quote mark or the second backslash,
                # one character for the pair.
                escaped = True
            elif quote == '"' and char == "\\" and index + 1 < length and command[index + 1] in EXPANSION:
                # Outside shlex's own rule -- posix shlex only special-cases
                # \" and \\ inside double quotes, so `"a\$b"` reaches shlex
                # as the four characters a\$b, backslash kept -- but bash's
                # wider double-quote escape set (\$, \`, \", \\, backslash-
                # newline) still neutralizes the character after it: git
                # receives the literal text $x for `"\$x"`, not a variable
                # bash expands. Both characters are masked, which is what
                # keeps shlex's own two-character length here, and the one
                # that would otherwise stay live loses the exemption.
                masked.append("Q")
                masked.append("Q")
                index += 2
                continue
            elif quote == '"' and char in EXPANSION:
                masked.append(char)
            else:
                masked.append("Q")
        elif char == "\\":
            escaped = True
        elif char in "'\"":
            if index + 1 < length and command[index + 1] == char:
                before = command[index - 1] if index > 0 else ""
                after = command[index + 2] if index + 2 < length else ""
                if (not before or before in boundary) and (not after or after in boundary):
                    masked.append("Q")
                index += 2
                continue
            quote = char
        else:
            masked.append(char)
        index += 1
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
    """The variable name in a `NAME=value` or `NAME+=value` prefix, or None.

    Both of bash's assignment operators are read, and the longer one first:
    `NAME+=value` is an assignment to bash as surely as `NAME=value` is, and
    splitting on `=` alone reads its name as `NAME+`, which is not an
    identifier -- so the word was not an assignment to step over, and the
    walk below took it for the name of the command.
    """
    word = bare(token)
    for operator in ASSIGNMENT_OPERATORS:
        name, found, _ = word.partition(operator)
        if found and name.isidentifier():
            return name
    return None


def after_redirection(segment: list[str], index: int) -> int:
    """The index just past the redirection whose operator is at `index`.

    A redirection is its operator and the token after it, the target. A
    target that opens a backtick substitution runs to the token that closes
    it, since shlex splits the substitution's words apart: `` >`printf
    cosign.pub` shellcheck contrib/aib `` still finds its name at shellcheck
    (review on #420, for the name walk before it).
    """
    index += 1  # the operator
    if index < len(segment):
        target = segment[index]
        index += 1  # the target
        if target.startswith("`") and not (len(target) > 1 and target.endswith("`")):
            while index < len(segment) and not segment[index - 1].endswith("`"):
                index += 1
    return index


class Invocation(NamedTuple):
    """A segment read as the command bash would run.

    `words` are the words the command receives, its own name first; `twins`
    are their masked copies, in the same order, for a check that has to know
    what bash would quote. `wrapped` says whether a wrapper word was stepped
    over, `name` is the token that named the command as it was typed (before
    a path or a substitution prefix was stripped from it), and `assignments`
    are the variables assigned before the name, which are the command's
    environment rather than its arguments.
    """

    words: list[str]
    twins: list[str]
    wrapped: bool
    name: str
    assignments: list[str]


def command_words(segment: list[str], twins: list[str] | None = None) -> Invocation:
    """The words of a segment that the command receives, in order.

    A redirection -- its operator, its target and a descriptor written before
    it -- is the shell's, not the command's, and bash lets it stand anywhere
    in the simple command, so `>cosign.pub shellcheck x` and `shellcheck
    >cosign.pub x` both come back as `['shellcheck', 'x']`; reading the `>`
    as the name left the whole segment unchecked. A target that opens a
    backtick substitution runs to the token that closes it, since shlex
    splits the substitution's words apart: `` >`printf x` git diff `` still
    finds its name at git. A leading assignment and a leading wrapper word
    (`time`, `command`, `env`) are stepped over too, since neither is part of
    the prefix an allow rule matches; the assignment is kept, because a
    variable set on a gated command is the environment it runs under.

    The first word left is the command's name, with a leading path stripped,
    so `/usr/bin/git` and `git` are one command. `wrapped` says whether a
    wrapper was stepped over: its own options come before the name it runs
    (`command -p shellcheck x`), so gated_prefix() and git_arguments() then
    look at every later word rather than only the first.

    This is the one walk. It used to be three -- one for git's name, one for
    the gated prefixes, one for shellcheck's operands -- which is how `env
    git diff --no-index a b` came to be read as a git invocation by none of
    them (#428).
    """
    if twins is None:
        twins = segment
    words: list[str] = []
    kept: list[str] = []
    assignments: list[str] = []
    wrapped = False
    name = ""
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
        if not words:
            assigned = assignment(token)
            if assigned is not None:
                assignments.append(assigned)
                index += 1
                continue
            if bare(token) in COMMAND_WRAPPERS:
                wrapped = True
                index += 1
                continue
            name = token
            words.append(bare(token).rsplit("/", 1)[-1])
        else:
            words.append(token)
        kept.append(twins[index])
        index += 1
    return Invocation(words, kept, wrapped, name, assignments)


def name_positions(invocation: Invocation) -> range:
    """Where in `words` the name of the command may stand.

    Behind a wrapper the name may follow the wrapper's own options (`command
    -p shellcheck x`, `env -i podman images`, `timeout 5 shellcheck x`), so
    every word is tried as the start; without one, only the first word names
    the command. Trying every word behind a wrapper can only over-refuse --
    an operand that happens to spell a gated name is read as the command --
    and modelling each wrapper's options is where the next hole hides.
    """
    if invocation.wrapped:
        return range(len(invocation.words))
    return range(min(len(invocation.words), 1))


def gated_prefix(invocation: Invocation) -> tuple[str, ...] | None:
    """The GATED_PREFIXES entry this invocation's leading words match, or None."""
    for start in name_positions(invocation):
        candidates = [
            bare(invocation.words[start]).rsplit("/", 1)[-1],
            *invocation.words[start + 1 :],
        ]
        for prefix in GATED_PREFIXES:
            if tuple(candidates[: len(prefix)]) == prefix:
                return prefix
    return None


def git_arguments(invocation: Invocation) -> list[str] | None:
    """The words a `git` invocation receives, or None when this is not one.

    The name is looked for exactly where gated_prefix() looks for one of
    its own, so `env git diff --no-index a b`, `nice git ...` and
    `/usr/bin/git ...` are the git invocation they run as. A redirection and
    its target are not in `words`, which is why `git diff HEAD </dev/null`
    is not read as a `/dev/null` operand.
    """
    for start in name_positions(invocation):
        if bare(invocation.words[start]).rsplit("/", 1)[-1] == "git":
            return invocation.words[start + 1 :]
    return None


def gated_name(words: list[str], start: int) -> str | None:
    """The gated command `words[start:]` runs, if it is one: `git`, or the
    GATED_PREFIXES entry its leading words match, spelled out. None
    otherwise."""
    bare_word = bare(words[start]).rsplit("/", 1)[-1]
    if bare_word == "git":
        return "git"
    candidates = [bare_word, *words[start + 1 :]]
    for prefix in GATED_PREFIXES:
        if tuple(candidates[: len(prefix)]) == prefix:
            return " ".join(prefix)
    return None


def command_start(invocation: Invocation) -> int | None:
    """The position in `words` where the gated command's own name was
    found, whether that is `git` or a GATED_PREFIXES entry, or None.

    gated_prefix() and git_arguments() each run this same search and stop
    at their own match; kept here as one search so a wrapper's own option
    words -- which stand at every position before this one when the
    invocation is wrapped -- can be told from the command's, for
    wrapper_chdir().
    """
    for start in name_positions(invocation):
        if gated_name(invocation.words, start) is not None:
            return start
    return None


def long_option_name(token: str) -> str | None:
    """The name half of a long-option word (`--chdir` of `--chdir=/tmp`),
    or None when this is not a long option at all -- `--` alone is
    excluded too, since it is the operand separator rather than an
    option with an empty name.
    """
    if not token.startswith("--") or token == "--":
        return None
    return token.split("=", 1)[0]


def long_option_abbreviates(name: str, full: str) -> bool:
    """Does `name` (a long option's own name, `--` included, no `=value`)
    reach `full` the way GNU getopt_long resolves an unambiguous prefix?
    The env callers have already picked a `full` no other option of the
    same program begins the same way, which is what makes the prefix
    unambiguous without walking that program's whole option table --
    refused_long() reads git's own abbreviated long options the same way.
    xargs_option_takes_next_word() walks xargs's whole table instead, and
    reads a prefix shared with an entry that takes a value as taking one.
    """
    return len(name) > 2 and full.startswith(name)


def xargs_option_takes_next_word(word: str) -> bool:
    """May xargs read the word after this option word as the option's value?

    Answered for the reading that finds the most commands, not only GNU's:
    a yes sends xargs_command_starts() down both readings, so a wrong yes
    can only over-refuse and a wrong no is a hole. See XARGS_FLAG_SHORT.
    """
    name = long_option_name(word)
    if name is not None:
        if "=" in word:
            return False
        return any(
            long_option_abbreviates(name, full) for full in XARGS_VALUED_LONG
        ) or not any(long_option_abbreviates(name, full) for full in XARGS_PLAIN_LONG)
    for index, letter in enumerate(word[1:], start=1):
        if letter in XARGS_FLAG_SHORT:
            continue
        if letter in XARGS_ATTACHED_SHORT:
            return False
        return index == len(word) - 1
    return False


def xargs_command_starts(words: list[str], index: int) -> list[int]:
    """Every position in `words` where the command run by the `xargs` at
    `index` may start.

    xargs stops reading options at its first word that is not one (GNU's
    getopt runs in `+` mode), and `--` ends them outright. An option that
    may take the next word as its value forks the walk -- that word read as
    the value, and read as the command -- so the answer is a list: `xargs
    -I {} git diff {}` has git at one reading and `{}` at the other.
    """
    starts: list[int] = []
    pending = [index + 1]
    seen: set[int] = set()
    while pending:
        position = pending.pop()
        if position >= len(words) or position in seen:
            continue
        seen.add(position)
        word = words[position]
        if word == "--":
            if position + 1 < len(words):
                starts.append(position + 1)
            continue
        if word.startswith("-") and word != "-":
            pending.append(position + 1)
            if xargs_option_takes_next_word(word):
                pending.append(position + 2)
            continue
        starts.append(position)
    return starts


def xargs_feeds(words: list[str], index: int) -> str | None:
    """The gated command the `xargs` at `words[index]` runs, or None.

    The command it runs may be another wrapper (`xargs nice git diff`,
    `xargs timeout 5 shellcheck`), and then the gated name is looked for at
    every later word, the way name_positions() looks behind a wrapper; or
    another xargs, which is read the same way this one is.
    """
    for start in xargs_command_starts(words, index):
        name = bare(words[start]).rsplit("/", 1)[-1]
        if name == "xargs":
            fed = xargs_feeds(words, start)
        elif name in COMMAND_WRAPPERS:
            fed = next(
                (
                    found
                    for later in range(start + 1, len(words))
                    if (found := gated_name(words, later)) is not None
                ),
                None,
            )
        else:
            fed = gated_name(words, start)
        if fed is not None:
            return fed
    return None


def xargs_fed_command(invocation: Invocation) -> str | None:
    """The gated command an `xargs` in this invocation runs, or None.

    xargs appends the words it reads from standard input, or from the file
    its `-a` names, to the command it runs, so `printf '%s\\n' /dev/null
    ./cosign.key | xargs git diff` hands git two operands that are not in
    this string, and Claude Code's matcher accepts `xargs git diff` for the
    `git diff:*` row. The xargs is looked for wherever command_words() looks
    for a name -- the first word, or behind a wrapper any word (`timeout 5
    xargs git diff`) -- and it may itself stand behind `nice` or `env`,
    which command_words() has already stepped over. An xargs whose command
    is not gated (`xargs echo`, `xargs grep -n git`) is None: no allow rule
    matches it, so Claude Code prompts.
    """
    for start in name_positions(invocation):
        if bare(invocation.words[start]).rsplit("/", 1)[-1] == "xargs":
            fed = xargs_feeds(invocation.words, start)
            if fed is not None:
                return fed
    return None


def env_short_option(token: str, letter: str) -> str | None:
    """Does this short-option word carry `letter` ("C" or "S"), and if so
    what value is attached to it in the same word (empty when the value
    is a separate word instead)? None when the word does not carry it --
    it is not a short-option word at all, or a different value-taking
    letter (ENV_VALUED_SHORT) claims the rest of the word first, the way
    refused_short() already stops its own walk at git's first value-taking
    letter.
    """
    if not token.startswith("-") or token.startswith("--"):
        return None
    for index, char in enumerate(token[1:], start=1):
        if char == letter:
            return token[index + 1 :]
        if char in ENV_VALUED_SHORT:
            return None
    return None


def wrapper_relocates(token: str) -> bool:
    """Is this wrapper option word GNU env's `-C`/`--chdir`, in any spelling
    env itself accepts?

    `token.split("=", 1)[0] in {"-C", "--chdir"}` -- an early exact-match
    test -- missed three spellings getopt gives env for free: the short
    option's argument attached with no separator at all (`-C/tmp/other`,
    not only `-C /tmp/other` or `-C=/tmp/other`, which env does not accept
    either but this scan need not reject), `-C` clustered behind one of
    env's own boolean options (`-iC/tmp/other` is `-i` then `-C/tmp/other`,
    and GNU env's own getopt_long clusters short options the way git's
    does), and any unambiguous abbreviation of the long option
    (`--chd=/tmp/other`, `--chdir` alone with the value a separate word).
    """
    name = long_option_name(token)
    if name is not None:
        return long_option_abbreviates(name, WRAPPER_RELOCATION_LONG)
    return env_short_option(token, "C") is not None



def wrapper_chdir(invocation: Invocation) -> str | None:
    """The wrapper option that relocates this invocation before its gated
    command runs, or None.

    Only the words ahead of command_start() are the wrapper's own --
    `env -C /tmp/other git diff` and `env --chdir=/tmp/other git diff`
    both put `git` at a later position than 0, and everything before it
    is env's, not git's. `-C` after the name is git's own diff option
    (`git diff -C`) and is left to global_refusal(), which only reads
    `-C` ahead of a subcommand -- there is none to be ahead of here, since
    this only runs when a wrapper was stepped over.

    Position 0 is read from `invocation.name` rather than
    `invocation.words[0]`: command_words() runs the first word through
    `bare().rsplit("/", 1)[-1]` to turn a path into a bare command name,
    and `--chdir=/tmp/other` has a `/` of its own -- the same walk would
    turn it into `other` and lose the option entirely. Every later
    position is stored unprocessed already.
    """
    if not invocation.wrapped:
        return None
    start = command_start(invocation)
    if start is None:
        return None
    for index in range(start):
        token = invocation.name if index == 0 else invocation.words[index]
        if wrapper_relocates(token):
            return token
    return None


def env_split_string_value(token: str) -> str | None:
    """The value attached to this word if it is any spelling of env's own
    `-S`/`--split-string` carrying it in the same word -- clustered or
    bare short (`-iS'...'`, `-S'...'`), the exact long option, or an
    unambiguous abbreviation of it (`--split-string='...'`, `--split='...'`)
    -- or None when the word carries no attached value this way: it may
    still be the option alone with its value the next word instead (see
    env_split_string_detached()), or an ordinary word altogether.
    """
    name = long_option_name(token)
    if name is not None:
        if long_option_abbreviates(name, WRAPPER_SPLIT_STRING_LONG) and "=" in token:
            return token.split("=", 1)[1]
        return None
    value = env_short_option(token, "S")
    return value if value else None


def env_split_string_detached(token: str) -> bool:
    """Is this word env's own `-S`/`--split-string`, in any spelling, with
    nothing attached to it -- so the next word is the string it re-splits?
    """
    name = long_option_name(token)
    if name is not None:
        return "=" not in token and long_option_abbreviates(name, WRAPPER_SPLIT_STRING_LONG)
    return env_short_option(token, "S") == ""


def assigned_environment(token: str) -> tuple[str, str] | None:
    """The REFUSED_ENVIRONMENT row this word assigns, or None.

    Every spelling that puts a variable in a command's environment writes the
    assignment as a word: on the gated command itself (`SHELLCHECK_OPTS=x
    shellcheck ...`), as an argument to a wrapper (`env GIT_DIR=x git ...`,
    with or without `-i`), or in an earlier command of the same string
    (`export GIT_EXTERNAL_DIFF=x; git diff`, and `declare -x` or `typeset -x`
    where the tokenizer sees them). So the test is the word, and every word of
    the command is tested -- the assignment stands before the command name, so
    there is no invocation to scope it to at the point it is read.

    Bash has two assignment operators and both are matched: `+=` appends, and
    appending to an unset variable creates it, so the append spelling reaches
    the environment as surely as `=` does.

    `env -S`, in every spelling env itself accepts -- clustered or bare
    short, the exact long option, or an unambiguous abbreviation of it --
    re-splits its argument into a command line of its own, which puts the
    assignment and the command it runs inside a single word; the word is
    split on whitespace and each piece tested, and the option's own
    spelling is stripped from the front first (env_split_string_value()).

    The token is the word with its quotes removed, so `SHELLCHECK_OPTS='-s
    bash'` and `env 'GIT_DIR'=/tmp/other` are found as readily as the bare
    form -- the second is not an assignment to bash at all, but `env` reads
    the argv string it becomes as one. A word that only quotes the assignment
    without making it (`grep 'SHELLCHECK_OPTS=' docs/SECURITY-AI.md`) is
    refused with the rest; a variable can be searched for by name alone.

    `readonly NAME=x` and a bare `declare NAME=x` do not export, so they reach
    no child; they are refused with the rest all the same, because the rule is
    the word that assigns the name rather than a model of which builtin
    exports. That direction over-refuses, which is the safe one.
    """
    word = bare(token)
    split_value = env_split_string_value(word)
    if split_value is not None:
        word = split_value
    for piece in word.split():
        name = assigned_name(piece)
        if name is None:
            continue
        for pattern, reach in REFUSED_ENVIRONMENT:
            if fnmatch.fnmatchcase(name, pattern):
                return name, reach
    return None


def assigned_name(piece: str) -> str | None:
    """The variable name a `NAME=`/`NAME+=` word assigns, or None.

    Looser than assignment(): `env 'NAME'=value` is not an assignment to bash
    at all, and the quotes are gone by the time this reads the word, so the
    name is taken from the text before the operator without asking whether
    bash would have honoured it. `env` reads the argv string it becomes as an
    assignment, which is the point.
    """
    for operator in ASSIGNMENT_OPERATORS:
        name, found, _ = piece.partition(operator)
        if found and name:
            return name
    return None


def env_split_escape(token: str) -> str | None:
    """The backslash escape in an `env -S`/`--split-string` argument this
    scan does not parse, or None.

    env's own splitting language treats a backslash specially before its
    own word split ever runs: `\\_` is a space that still separates two
    words the way an unescaped one does (documented for a shebang line,
    where runs of whitespace collapse to one before env ever sees them),
    `\\ ` (backslash space) folds the space it precedes into the word
    instead of splitting on it, and `\\c`, `\\#`, `\\$`, a quote and the
    control-character spellings (`\\n`, `\\t`, ...) each rewrite the string
    before that split runs too. word.split() in assigned_environment()
    only recognises the whitespace already sitting in the token, so
    `env -S'FOO=x\\_GIT_EXTERNAL_DIFF=./evil\\_git diff'` -- which env
    itself splits into `FOO=x`, `GIT_EXTERNAL_DIFF=./evil`, `git` and
    `diff` -- arrives at word.split() as one merged piece whose assigned
    name is only `FOO`, and the refused row never fires while the git
    invocation env actually runs carries the driver. Every spelling
    env_split_string_value() reads carries the same risk, clustered and
    abbreviated ones included.

    Implementing env's splitting language is not attempted here: a split
    string carrying a backslash is refused outright instead, since a name
    this scan cannot see split out is a name it cannot clear.
    """
    word = bare(token)
    split_value = env_split_string_value(word)
    if split_value is None:
        return None
    return split_value if "\\" in split_value else None


def denied_read_shape(path: str) -> bool:
    """Does this path's basename carry one of the Read(...) deny shapes?"""
    return any(
        fnmatch.fnmatchcase(path.rsplit("/", 1)[-1], shape)
        for shape in DENIED_READ_SHAPES
    )


def shellcheck_arguments(invocation: Invocation) -> list[tuple[str, str]]:
    """The words a `shellcheck` invocation receives, each with its twin.

    Everything after the word naming shellcheck is returned, which is where
    gated_prefix() found the name -- behind a wrapper that may be its own
    options (`command -p shellcheck x`). The masked twin travels with the
    word so a check that has to know what bash would quote can read it: shlex
    hands back the same `*.sh` for `'*.sh'`.
    """
    for position, token in enumerate(invocation.words):
        if bare(token).rsplit("/", 1)[-1] == "shellcheck":
            return list(
                zip(
                    invocation.words[position + 1 :],
                    invocation.twins[position + 1 :],
                    strict=True,
                )
            )
    return []


def reading_redirections(
    segment: list[str], twins: list[str]
) -> list[tuple[str, str]]:
    """The targets of the redirections in `segment` that open a path for
    reading, each with its masked twin.

    Only a bare `<` (with or without a descriptor: `<f`, `0<f`) opens a
    path. `<<` reads a here-document, `<<<` a here-string, and `<&` and `<>`
    duplicate or open read-write, which writing_redirection() already
    refuses. A here-string's word is content, not a path, and cannot name a
    file without a substitution, which is refused before this runs.
    """
    targets: list[tuple[str, str]] = []
    for index, token in enumerate(segment):
        if token != "<" or index + 1 >= len(segment):
            continue
        targets.append((segment[index + 1], twins[index + 1]))
    return targets


def shellcheck_refusal(
    invocation: Invocation, segment: list[str], twins: list[str]
) -> str | None:
    """Why this shellcheck invocation must not run, or None.

    Everything here is refused by default: a word the scan does not recognise
    as an option is treated as a path and checked, so an option this list
    forgets costs a refused lint run rather than an unwatched read.

    A `-` operand makes ShellCheck read standard input, and `shellcheck - <
    .env` prints the file back exactly as `shellcheck ./.env` would, so the
    target of an input redirection is checked as an operand: it must stay
    inside the checkout, carry none of the deny shapes, and be spelled out
    (no brace, no leading `~`, no glob -- bash refuses an ambiguous redirect
    itself, but a glob naming exactly one denied file is not ambiguous).
    """
    for target, twin in reading_redirections(segment, twins):
        if target == "/dev/null":
            continue  # nothing to print back; `</dev/null` is how a session says "no stdin"
        if (
            brace_would_expand(target)
            or twin.startswith("~")
            or any(char in twin for char in GLOB)
            or unsafe_operand(target)
            or denied_read_shape(target)
        ):
            return (
                f"<{target} feeds shellcheck a file on standard input, and shellcheck "
                "prints the source line above every diagnostic it reports, so a "
                "redirection from a file that leaves the checkout, or that carries "
                "one of the shapes the Read(./cosign.key), Read(./.env) and "
                "Read(**/*.pem) deny rules in .claude/settings.json name, prints that "
                "file back exactly as naming it as an operand would; redirect from a "
                "script inside the checkout, spelled out in full"
            )
    skip_value = False
    for token, twin in shellcheck_arguments(invocation):
        if skip_value:
            skip_value = False
            continue
        if brace_would_expand(token) or twin.startswith("~"):
            return (
                f"{token} is not the word shellcheck would receive -- bash rewrites it "
                "first, expanding a brace into several words and an unquoted leading ~ "
                "into a home directory outside the checkout -- and this gate reads words "
                "as typed, so the path it checked would not be the path shellcheck "
                "opened; spell every path out in full, relative to the checkout (a "
                "glob is expanded here and each file it names is checked)"
            )
        if token in SHELLCHECK_VALUE_OPTIONS:
            skip_value = True
            continue
        if token == "-" or token.startswith("-"):
            continue
        candidates = [token]
        if any(char in twin for char in GLOB) and not unsafe_operand(token):
            # Matching nothing leaves the pattern as the word bash passes on,
            # which shellcheck then fails to open; that is the literal, and it
            # is checked as one.
            candidates = sorted(glob.glob(token)) or [token]
        for candidate in candidates:
            if unsafe_operand(candidate) or denied_read_shape(candidate):
                return (
                    f"{candidate} is a path shellcheck would print back: it prints the "
                    "source line above every diagnostic it reports, so an operand that "
                    "leaves the checkout, or that carries one of the shapes the "
                    "Read(./cosign.key), Read(./.env) and Read(**/*.pem) deny rules in "
                    ".claude/settings.json name, hands the file's own lines to the model "
                    "-- every unexported NAME=value line of a .env, the base64 body of a "
                    "signing key -- past rules that gate the Read tool and say nothing "
                    "about what an allow-listed Bash command opens; lint this "
                    "repository's own scripts instead"
                )
    return None


# The options that tell `just` which file to parse. `--fmt --check` prints the
# source line of the first parse error it hits, so the file these name is read
# back to the model a line at a time: `-f` and `--justfile` in all three
# spellings bash hands over (separate word, `=`, attached to the short form),
# and `-d`/`--working-directory`, which names the directory `just` then searches
# for a `justfile` of its own.
JUST_PATH_OPTIONS = frozenset({"-f", "--justfile", "-d", "--working-directory"})


def just_path_values(invocation: Invocation) -> list[tuple[str, str]]:
    """The paths a `just --fmt --check` invocation would open, with their twins.

    Every spelling of `-f`/`--justfile`/`-d`/`--working-directory` bash passes
    through: the value as a separate word, `--justfile=PATH`, and `-fPATH`
    attached to the short form. Positional words are recipe arguments rather
    than paths and are left alone -- `--fmt` runs no recipe.
    """
    values: list[tuple[str, str]] = []
    words: list[tuple[str, str]] = []
    for position, token in enumerate(invocation.words):
        if bare(token).rsplit("/", 1)[-1] == "just":
            words = list(
                zip(
                    invocation.words[position + 1 :],
                    invocation.twins[position + 1 :],
                    strict=True,
                )
            )
            break
    expect_value = False
    for token, twin in words:
        if expect_value:
            expect_value = False
            values.append((token, twin))
            continue
        if token in JUST_PATH_OPTIONS:
            expect_value = True
            continue
        if "=" in token and token.split("=", 1)[0] in JUST_PATH_OPTIONS:
            index = token.index("=") + 1
            values.append((token.split("=", 1)[1], twin[index:]))
            continue
        if len(token) > 2 and token[:2] in JUST_PATH_OPTIONS:
            values.append((token[2:], twin[2:]))
    return values


def just_refusal(
    invocation: Invocation, segment: list[str], twins: list[str]
) -> str | None:
    """Why this `just --fmt --check` invocation must not run, or None.

    `just` reports a parse error with the offending source line printed under
    it, so `just --fmt --check --justfile ./.env` prints that file's first
    `NAME=value` line, value included, past the `Read(./.env)` deny rule --
    leading comments and blank lines parse, so the line it reaches is the
    first one that carries a secret rather than a header. A `-` or
    `/dev/stdin` justfile reads standard input, so the target of an input
    redirection is checked the way shellcheck_refusal() checks one.
    """
    for target, twin in reading_redirections(segment, twins):
        if target == "/dev/null":
            continue
        if (
            brace_would_expand(target)
            or twin.startswith("~")
            or any(char in twin for char in GLOB)
            or unsafe_operand(target)
            or denied_read_shape(target)
        ):
            return (
                f"<{target} feeds `just --fmt --check` a file on standard input, which "
                "it parses as a justfile and prints the offending source line back "
                "from -- `just --fmt --check --justfile /dev/stdin < ./.env` and "
                "`-f -` both print the line the Read(./cosign.key), Read(./.env) and "
                "Read(**/*.pem) deny rules exist to keep out of the transcript; format "
                "a justfile inside the checkout, named as a path"
            )
    for token, twin in just_path_values(invocation):
        if brace_would_expand(token) or twin.startswith("~"):
            return (
                f"{token} is not the word just would receive -- bash rewrites it first, "
                "expanding a brace into several words and an unquoted leading ~ into a "
                "home directory outside the checkout -- and this gate reads words as "
                "typed, so the path it checked would not be the path just opened; "
                "spell every path out in full, relative to the checkout"
            )
        candidates = [token]
        if any(char in twin for char in GLOB) and not unsafe_operand(token):
            candidates = sorted(glob.glob(token)) or [token]
        for candidate in candidates:
            if (
                candidate in {"-", "/dev/stdin"}
                or candidate.startswith("/dev/fd/")
                or unsafe_operand(candidate)
                or denied_read_shape(candidate)
            ):
                return (
                    f"{candidate} is a path just would print back: `--fmt --check` "
                    "reports a parse error with the source line under it, so a justfile "
                    "that leaves the checkout, that is read from standard input, or "
                    "that carries one of the shapes the Read(./cosign.key), "
                    "Read(./.env) and Read(**/*.pem) deny rules in "
                    ".claude/settings.json name, hands that file's own line to the "
                    "model -- the first NAME=value line of a .env arrives whole, value "
                    "included -- past rules that gate the Read tool and say nothing "
                    "about what an allow-listed Bash command opens; format this "
                    "repository's own justfiles instead"
                )
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
        masked = tokenize(mask_quotes_stripped(command))
    except ValueError:
        # Unbalanced quoting. What the shell would do with it cannot be read
        # here, so it is refused rather than guessed at.
        return "the command cannot be parsed as shell words, so its git arguments cannot be checked"
    if len(tokens) != len(masked):
        # The masked copy split differently, so which tokens are separators
        # cannot be told; refused rather than guessed at.
        return "the command's quoting cannot be matched to its words, so its git arguments cannot be checked"
    for token, twin in zip(tokens, masked, strict=True):
        # Read before the segments are walked, because the word need not be
        # in a segment that names a gated command at all: an `export` in an
        # earlier segment reaches the later shellcheck just as a leading
        # assignment does, and an `export` in a string that runs nothing
        # gated reaches the *next* Bash call, whose command this hook is not
        # reading yet.
        if runtime_assignment(token, twin):
            return (
                f"{token} carries a `$` or a backtick sitting next to an `=`, which is "
                "bash rebuilding the word before the command runs rather than the "
                "literal spelling this scan reads -- `export "
                "GIT_EXTERNAL_DIFF$'=./evil'` concatenates the bareword and the ANSI-C "
                "quote into one word and exports GIT_EXTERNAL_DIFF=./evil, and "
                "assigned_environment() would only find the name up to the `=` sitting "
                "inside the quote. A word whose assigned name this scan cannot read as "
                "typed and whose known prefix could still be one of REFUSED_ENVIRONMENT's "
                "names is refused rather than assumed clear of it; write the assignment "
                "as a literal NAME=value word instead"
            )
        split_escape = env_split_escape(token)
        if split_escape is not None:
            return (
                f"{token} is env's -S/--split-string argument and carries a backslash "
                f"escape ({split_escape!r}) -- `\\_`, `\\ `, `\\c`, `\\#`, `\\$`, a quote, "
                "or a control-character spelling -- which env's own splitting language "
                "reads before this scan's word.split() ever runs, so a word boundary (or "
                "an assignment word.split() does not see split out) can hide inside it: "
                "`\\_` separates two words the way an unescaped space does, and "
                "word.split() does not split there. Write the assignment and the command "
                "as separate words instead of inside a split string with an escape in it"
            )
        assigned = assigned_environment(token)
        if assigned is not None:
            name, reach = assigned
            return (
                f"{name} {reach}, so the command is no longer the read or the lint the "
                "allow list describes. The assignment stands before the command name, so "
                "it is refused wherever it is written -- on the command, behind env "
                "including its -i, -S and --split-string forms, or as an export, declare "
                "or typeset in another command of the same string -- in both of bash's "
                "assignment operators, since += on an unset variable creates it, and "
                "whatever value it carries, since nothing in this repository sets it. The "
                "Bash tool's shell outlives one call, so an export left unrefused here "
                "would be in the environment of the next call's command; pass what the "
                "command needs after its name instead"
            )
    for segment, twins in segments(tokens, masked):
        if segment and bare(segment[0]) == "env":
            # A detached env -S/--split-string, in any spelling env
            # accepts (clustered, abbreviated) -- its own word, not
            # `-S'...'` or `--split-string='...'` attached to one word:
            # the string env re-splits is the word right after it, and a
            # backslash in that word is env's splitting language the same
            # as it is when the option and the string share a word --
            # env_split_escape() only reads the attached spellings, since
            # that is the one where the token itself carries the option's
            # name. Scoped to a segment whose own first word is `env`,
            # since `-S` is also git's pickaxe option (`git log -S`,
            # `git diff -S`) and belongs to whatever command reads it --
            # arming on the bare word regardless of position let
            # `git log -S 'foo\bar' --oneline`, an ordinary pickaxe search
            # with a backslash in it, refuse for a reach it never has.
            for index, word in enumerate(segment[1:], start=1):
                if env_split_string_detached(bare(word)) and index + 1 < len(segment):
                    following = segment[index + 1]
                    if "\\" in bare(following):
                        return (
                            f"{following} is the word right after a detached env "
                            "-S/--split-string and carries a backslash escape -- "
                            "`\\_`, `\\ `, `\\c`, `\\#`, `\\$`, a quote, or a "
                            "control-character spelling -- which env's own splitting "
                            "language reads before this scan's word.split() ever runs, "
                            "the same reach env_split_escape() already refuses when the "
                            "option and the string share one word (`-S'...'`); env reads "
                            "its argument the same way whichever word carries the "
                            "option. Write the assignment and the command as separate "
                            "words instead of inside a split string with an escape in it"
                        )
        invocation = command_words(segment, twins)
        if invocation.words and invocation.words[0] in EXPORT_FAMILY:
            # Every word after the name is a candidate assignment
            # assigned_environment() already tests by content; this is
            # the one shape -- a live `$` or backtick with nothing before
            # it -- content alone cannot decide (see
            # opaque_assignment_prefix()), so it is scoped to exactly the
            # position that reaches a child's environment: this family's
            # own arguments, read as NAME=value only after bash has
            # already expanded them.
            for word, twin in zip(invocation.words[1:], invocation.twins[1:], strict=True):
                if opaque_assignment_prefix(word, twin):
                    return (
                        f"{word} opens with a `$` or a backtick bash expands before "
                        f"{invocation.words[0]} ever reads it as an argument, so the name "
                        "it assigns cannot be read here at all -- `export "
                        "$'GIT_EXTERNAL_DIFF=./evil'` is one word to bash, with nothing "
                        "before the `$` for a plain-identifier prefix test to read, and "
                        "it exports exactly what `export GIT_EXTERNAL_DIFF=./evil` does. "
                        "A word whose assigned name this scan cannot read as typed at "
                        "all is refused rather than assumed clear of REFUSED_ENVIRONMENT; "
                        "write the assignment as a literal NAME=value word instead"
                    )
        for position in name_positions(invocation):
            candidate = (
                invocation.name if position == 0 else invocation.words[position]
            )
            if brace_would_expand(candidate):
                return (
                    f"{candidate} carries a brace that bash expands before the command "
                    "runs, and it is a word this scan reads as the command's name behind "
                    "a wrapper's own options: `{,git} diff --no-index a b` drops the "
                    "empty word and runs git, `{,shellcheck} ./.env` runs the linter, and "
                    "`env -i {,git} diff --no-index a b` hides the same trick behind "
                    "env's own `-i` -- so which command this segment runs cannot be read "
                    "from the word as typed; write the command's name out in full"
                )
        relocated = wrapper_chdir(invocation)
        if relocated is not None:
            return (
                f"{relocated} changes the working directory a wrapper's command runs "
                "from before this scan ever decides whether that command is `git` or "
                "another gated name -- GNU env documents `-C DIR`/`--chdir=DIR` for "
                "exactly that -- so every operand read after it is read as though the "
                "command still ran inside this checkout while it did not: `env -C "
                "/tmp/other git diff` reaches the allow-listed `git diff` and prints "
                "another repository's unstaged content past operands that name nothing "
                "this hook refuses. A wrapper that relocates its command is refused "
                "outright rather than validated against a path this scan cannot resolve"
            )
        fed = xargs_fed_command(invocation)
        if fed is not None:
            return (
                f"xargs runs `{fed}` with words it reads from standard input (or "
                "from the file -a names) added to the ones written here, so the "
                f"operands `{fed}` receives are not in this string and none of them "
                "can be checked -- `xargs git diff <list.txt` prints a key when "
                "list.txt names /dev/null and ./cosign.key -- and an allow rule for "
                f"`{fed}` matches it behind xargs as readily as on its own, so nothing "
                "prompts either. Name the operands in the command itself instead "
                "(xargs in front of a command no allow rule covers, such as `xargs "
                "echo`, is not refused)"
            )
        prefix = gated_prefix(invocation)
        if prefix is not None:
            if (
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
            redirection = writing_redirection(segment)
            if redirection:
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
            if invocation.assignments:
                # Any name, not only a REFUSED_ENVIRONMENT one: a variable set
                # on a gated command is nothing an ordinary lint or inspection
                # run needs, so the position is enough to refuse it and the
                # name list does not have to be complete for this spelling.
                # The name list is what covers the spellings that are *not* in
                # this position -- an `export` in another command of the
                # string -- where a rule on any name would refuse `x=1; podman
                # images` and every other ordinary compound command.
                return (
                    f"{invocation.assignments[0]}= before `{' '.join(prefix)}` is an "
                    "environment the command runs under, and for these commands that "
                    "changes what runs or where it goes -- PYTHONPATH= puts a module of "
                    "its own ahead of the audit's imports, LD_PRELOAD= loads code before "
                    "a line is linted, GH_HOST= sends the token elsewhere, "
                    "CONTAINERS_CONF= re-points podman; run the command without the "
                    "assignment (a git invocation is not affected by this rule, and the "
                    "names that reach one are refused by REFUSED_ENVIRONMENT wherever "
                    "they are written)"
                )
            if prefix == ("shellcheck",):
                reason = shellcheck_refusal(invocation, segment, twins)
                if reason is not None:
                    return reason
            if prefix == ("just", "--fmt", "--check"):
                reason = just_refusal(invocation, segment, twins)
                if reason is not None:
                    return reason
            continue
        arguments = git_arguments(invocation)
        if arguments is None:
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

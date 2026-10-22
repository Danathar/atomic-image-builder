# The shared gate refusal corpus

Every repository in this fleet has a PreToolUse hook that stops an agent's
commands from reading `.env`, writing a file, or running a program the allow
list never named, through an allow-listed command (`git diff`, `git log`,
`gh pr view` and the like) or behind any wrapper in front of one, whether or
not Claude Code would ask first. A session in bypass or auto permission mode
asks for nothing, so the hook is the boundary there and the prompt is not
([#772](https://github.com/Danathar/atomic-image-builder/issues/772)).
Each repository wrote its own copy, in bash or Python, and the copies have
drifted apart. A bypass fixed in one copy is not fixed in the others, and
nothing fails when a repository misses one
([#609](https://github.com/Danathar/atomic-image-builder/issues/609)).

[`tests/fixtures/gate-refusal-corpus.json`](../tests/fixtures/gate-refusal-corpus.json)
is the one table they share. Each row is a command and the verdict every gate
has to reach. A repository runs the rows its own allow list makes reachable,
so a bypass found anywhere becomes one row, and every repository that allows
the command fails its tests until its gate refuses it.

This repository holds the canonical copy. Change the table here first.

## A row

```json
{
  "id": "stdin-redirect",
  "class": "redirection",
  "verdict": "refuse",
  "requires": ["git log"],
  "command": "git log --stdin <.env",
  "why": "git log --stdin prints the first line of the file on standard input"
}
```

- `id`: unique and stable. Other repositories name rows by it.
- `class`: the bypass family, so a reader can see which rows belong together.
- `verdict`: `refuse` or `allow`. Ordinary commands are rows too: a gate that
  refuses everything passes every `refuse` row and gets switched off.
- `requires`: the command prefixes the row depends on. A repository runs the
  row only when each prefix is covered by a wildcard `Bash(...)` allow rule in
  its `.claude/settings.json`. `Bash(git diff:*)`, `Bash(git diff *)` and
  `Bash(git diff*)` all cover `git diff`; a rule with no wildcard covers no
  prefix. A row that needs `gh pr view` does not run where `gh pr view` is not
  allowed, because Claude Code asks before that command anyway.
- `wraps` (optional): the program the command starts with, when that program
  is a wrapper no allow rule covers, such as `flock` in
  `flock /tmp/l git diff --no-index /dev/null .env`. The row still runs
  wherever `requires` is covered: Claude Code asks before the wrapped command
  in the default mode, but not in bypass or auto mode, and every gate has to
  refuse it in all of them. `wraps` is never one of the `requires` prefixes, or the
  row would run only where the wrapper itself is allowed. A row without it is
  read exactly as before.
- `command`: the command string exactly as the hook receives it.
- `why`: what the command reaches, in one line, so a failure explains itself.

Rows read `.env`, because every repository in the fleet denies it. A
repository-specific secret (`cosign.key`, `secrets.yaml`) belongs in that
repository's own gate tests.

A row is decided by running the hook the way Claude Code does: the command
`.claude/settings.json` registers, with `{"tool_name": "Bash", "tool_input":
{"command": ...}}` on standard input. Exit status 2, or a `"deny"` decision on
standard output, is a refusal. Exit status 0 with no decision is an allow.

## Adding a row

1. Add it here, in `tests/fixtures/gate-refusal-corpus.json`.
2. Run `python3 -m unittest tests.test_gate_refusal_corpus`. If this
   repository's gate lets the command through, fix the gate in the same pull
   request, or list the row's id in that test's `KNOWN_GAPS` and open an
   issue to close it.
3. Copy the new file into each other repository that allows the command, and
   fix any gate that fails there, or list the row as a known gap there.

A known gap is a refuse row the gate lets through today. The test asserts
that it still gets through, so the change that closes it fails until its id
is removed, and a row that is not listed cannot regress silently. This
repository lists six, the `name spelling` rows below that its gate does not
refuse yet. The `wrapper-*` rows with a `wraps` field were added
from the matrix in
[#772](https://github.com/Danathar/atomic-image-builder/issues/772), which
ran each one against every gate in the fleet on 2026-10-20; this gate now
refuses a gated command behind `flock`, `watch`, `taskset`, `chrt`, `find`,
`unshare` or `ionice`, and inside a command string such as `env -S '...'`,
whatever its arguments are.

The `unlisted-*` rows put a program no gate lists in front of the command:
`setarch`, `prlimit`, `strace`, and `script -c`, which carries the command
as a quoted string. Since
[#781](https://github.com/Danathar/atomic-image-builder/pull/781) this gate
reads a gated command behind any program, not only the wrappers it names,
and `allow-unlisted-plain-diff` pins the other half of that rule: a command
that passes alone still passes behind one. Three more split the name inside
the string the way the shell joins it back together: `g""it`, `gi\t` and
`$'git'` all run git. A gate that looks for a gated name in the string only
as typed misses them, so it also has to look with the quotes, backslashes
and `$` removed.

The `name spelling` rows
([#790](https://github.com/Danathar/atomic-image-builder/issues/790)) write
the name `git` the ways the shell turns back into it before it runs anything:
a brace group (`{git,}`, whose empty alternative the shell drops), a wildcard
in a path (`/usr/bin/g?t`, which the shell matches to `/usr/bin/git`),
ANSI-C quoting (`$'git'`, and `$'\x67it'` with an escape) and locale
quoting (`$"git"`). The brace and wildcard rows also run behind `setarch`, a
program no gate lists. A gate that compares the command word as typed lets
them all through. This gate refuses `name-brace` only, so the other six are
known gaps here until the gate reads a name the way the shell does.

Before the corpus was first added, each row was checked against all six gates
at their `main` heads on 2026-09-30, and all six reached the verdict in the
table. The one disagreement found, `git diff --ext-diff`, is refused here and
allowed elsewhere. It is left out of the table: whether the other gates should
refuse it is a separate decision.

## Taking the corpus in another repository

Each repository has to keep a working gate on its own, for a contributor who
clones only that repository. So each one keeps a copy of the file rather than
fetching it:

- Copy the file to the same path, `tests/fixtures/gate-refusal-corpus.json`.
- Run every row that repository's allow list makes reachable through its own
  hook, as [`tests/test_gate_refusal_corpus.py`](../tests/test_gate_refusal_corpus.py)
  does here. The rules above are the whole contract; a bash repository can
  read the file with `jq`.
- Pin the file's SHA-256 in that test, so the copy cannot be edited locally.
  To change a row, change it here and copy it again.
- A runner that checks a row's fields has to accept the optional `wraps`
  field. One that checks a row names what it requires reads the command
  the way the shell does: quotes and backslashes removed, `$'...'` decoded,
  `$"..."` read as `"..."`, unquoted brace groups expanded, and an unquoted
  wildcard in a path matched against the name by its last part, as the
  `name-*` rows need. A wrapped row's words are read that way a second
  time, as `unlisted-script-split-quote` needs. `inner_words()` in
  [`tests/test_gate_refusal_corpus.py`](../tests/test_gate_refusal_corpus.py)
  is the reference. A gate that lets a row through lists
  it as a known gap, as akmods does, rather than skipping it.

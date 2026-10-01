# The shared gate refusal corpus

Every repository in this fleet has a PreToolUse hook that stops an agent's
allow-listed commands (`git diff`, `git log`, `gh pr view` and the like) from
reading `.env`, writing a file, or running a program the allow list never
named. Each repository wrote its own copy, in bash or Python, and the copies
have drifted apart. A bypass fixed in one copy is not fixed in the others, and
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
   request.
3. Copy the new file into each other repository that allows the command, and
   fix any gate that fails there.

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

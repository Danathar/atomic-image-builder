# CLAUDE.md

Claude Code reads this file automatically at the start of a session in this
repo. It is deliberately a signpost and not a second set of instructions.

**Read [`.github/copilot-instructions.md`](.github/copilot-instructions.md).**
That is the canonical brief for working here: what the project is, the checks
that gate a change, and the handful of things that are easy to get wrong in
this repo specifically. It points on to ARCHITECTURE.md, CONTRIBUTING.md,
MAINTAINER.md and maintenance_notes.txt.

Two things in this directory are worth knowing about before you start:

- [`.claude/skills/`](.claude/skills/) has packaged procedures. `verify-change`
  runs the local gate in the order that fails fastest and says what each result
  means, including the two checks that are easy to assume are covered and are
  not.
- [`.claude/memory/corrections.md`](.claude/memory/corrections.md) records
  things that were done wrong here and what the repo settled on instead.
  Several are mistakes worth not repeating from a cold start, because the
  correct answer looks wrong until you know why.
- [`.claude/checkpoint.md`](.claude/checkpoint.md) is what the last session
  handed on: decisions in force and their reasoning, dated. It deliberately
  holds no current-state claims, so look those up rather than reading them
  there.

[docs/reflections/](docs/reflections/) is the third and longest-grained of
these: retrospectives on finished work, written once the outcome is known.
Its README states the boundary between all three.

Nothing from the canonical brief is repeated here, and a test enforces that.
One place to correct beats four places that agree until they do not.

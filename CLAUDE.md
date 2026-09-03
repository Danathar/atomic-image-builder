# CLAUDE.md

Claude Code reads this file automatically at the start of a session in this
repo, and it is the only file here that gets read without anyone choosing
to. Mostly a signpost, plus the four conventions too costly to leave
behind a link.

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

## The four that reach other people

Everything else is behind the link above, which costs a tool call an agent may
not spend. These four do not: being wrong about them ships to repositories
this project's users create, or cannot be undone. They are a verbatim mirror
of the canonical brief and a test fails if they drift from it, so correct them
there and re-run the suite rather than editing them here.

<!-- mirror:copilot-instructions start -->
**Every `uses:` in a workflow must be covered by `ACTION_PINS` or
`ACTION_REF_PINS`** in `atomic_image_builder.py`, or `maintenance_audit.py`
fails the build. Those tables ship to generated repos, so a linter action this
repo alone would run does not belong in them; pin the binary by version and
checksum instead, the way `ci.yml` does with hadolint.

**`template_snapshots/` is vendored.** It is a pinned copy of upstream
(`ublue-os/image-template`, `blue-build/template`). Do not reformat it, fix its
lint, or hand-edit it to make a check pass. It is refreshed as a unit, and the
weekly audit tracks its drift.

**Never rename `TOOL_SLUG`.** It feeds `STATE_FILE`
(`.atomic-image-builder.json`), which is written into every managed repo and is
how the tool recognises repos it created. Renaming it orphans all of them.
`TOOL_COMMAND` is the separate constant that names the command, and is safe.

**The coverage threshold is not a literal.** It lives in
`.coverage-thresholds.json`; `ci.yml` reads it with `jq`. Writing
`--fail-under=<number>` into the workflow fails a test, because a literal
silently wins over the file it reads. If you change the gate, change that file
and run the suite — a doc still quoting the old number fails too.
<!-- mirror:copilot-instructions end -->

Nothing outside that block is repeated from the canonical brief, and a test
enforces both halves: the mirror must match, and everything else must not
duplicate. One place to correct beats four places that agree until they do not.

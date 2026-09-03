# Checkpoint

What one session hands the next. Read
[`.github/copilot-instructions.md`](../.github/copilot-instructions.md) first
for how the repo works; this file is only the things that are true right now
and are not recoverable from the code.

## Why this file cannot go stale

A session summary normally rots: it records what was true on a Tuesday and
nothing says when it stops being true. This one is built so there is nothing
to rot.

**It holds no current-state claims.** Anything that changes on its own is not
written down here, it is looked up:

```bash
gh issue list --state open                       # what is open
gh pr list --state open                          # what is in flight
git log --oneline origin/main -15                # what landed
gh run list --workflow maintenance-audit.yml -L 5  # last audit
```

**Everything it does hold is a dated historical fact.** "On 2026-09-03 we
decided X because Y" stays true forever, whatever happens to X afterwards.
That is the same reason
[`.claude/memory/corrections.md`](memory/corrections.md) does not rot, and the
two are deliberately different: corrections are things done *wrong* and put
right, while this is things *decided* and the reasoning that would otherwise be
lost.

A test asserts every entry below carries a date, because an undated entry is
how the rot starts.

## Decisions in force

**2026-09-03 — ACMM issues are filename checks, so fix the real defect.**
The `[ACMM ...]` issues from `danathar-atomic-hive` test whether a *filename*
exists, not whether the capability does. Three L0 issues were false negatives:
the repo already had a ruff config (as `.ruff.toml`), a coverage gate (inline
in `ci.yml`) and end-to-end tests (also inline). Each was answered by fixing
the real defect behind the missing filename rather than adding a placeholder.

**2026-09-03 — one canonical agent brief, everything else points at it.**
Four criteria wanted a file telling an agent how to work here.
`.github/copilot-instructions.md` carries the content; `CLAUDE.md`,
`AGENTS.md`, `.cursor/`, `.github/prompts/` and `.claude/skills/` defer to it.
A test enforces that, comparing prose and ignoring fenced code blocks, since a
skill that tells you which commands to run has to contain them.

**2026-09-03 — `settings.json` is shared, `settings.local.json` is personal.**
`.gitignore` excludes `.claude/*` and re-includes only what is meant to be
reviewed. Personal allowances belong in the local file, which is never shared.

**2026-09-03 — `AGENTS.md` is the operator's document, committed unchanged.**
It is addressed to one model by name and covers conduct rather than
conventions. It was committed as its author wrote it, with one paragraph added
naming the boundary between it and the canonical brief.

## Declined, and why it will come back

**2026-09-03 — a committed point-in-time session summary was declined twice**
(#169, #184) on the grounds that it is stale on arrival with nothing to signal
when it stops being true, and that `.claude/memory/` already carries the
durable half. It was then requested (#186) and built as this file, which
answers the objection by design instead of by detection.

Anything at a path the evaluation does not check will be re-filed on every
run. That happened here three times for one criterion. Before treating such an
issue as new work, check whether the capability already exists under another
name.

## Recurring surprises

**2026-09-03 — the ACMM evaluation can run mid-merge and re-file satisfied
criteria.** Nine issues were opened for files that were already on `main`,
timestamped between merges. Verify against each issue's *own* accepted-path
list before acting; matching on criterion ID alone would have mis-closed one
of the nine as completed when nothing satisfied it.

**2026-09-03 — a guard written before the thing it guards is a hypothesis.**
The agent-guidance drift check had to be corrected three times, each time
because the next file revealed the check was wrong rather than the file. It
banned words its own subject matter required, then compared code blocks that
are legitimately repeated, then walked the working tree and failed on an
untracked personal file. All three passed their own suite first.

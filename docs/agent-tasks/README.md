# Agent tasks

How to trace a change in this repository back to the agent task that produced
it, and dated ledgers of what that trace found.

Most pull requests here are opened by coding agents working from an issue.
Each one leaves three marks, and together they answer "which task made this
change, and under what instructions". None of them is new; this page is where
they are written down.

Start from [`.github/copilot-instructions.md`](../../.github/copilot-instructions.md)
for how the repo works. This directory is a record, not a brief.

## The three marks

**The pull request signature.** An agent-written pull request body ends with
a line beginning `— hive:` that names the agent role, the backend, and the
model. This is the reliable signal: the pull request *author* is not. An
agent that pushes with the maintainer's token opens its pull request as
`Danathar`, so counting the app account alone undercounts agent work by
about a third (see the first ledger below).

```bash
gh pr view <n> --repo Danathar/atomic-image-builder --json body --jq '.body | capture("— hive: (?<sig>.*)").sig'
```

**The branch prefix.** An agent's branch is named `<role>/<slug>`, where the
role is the one in its signature: `quality/`, `scanner/`, `sec/`, `fix/`,
`arch/`, `guide/`, `strategy/`. The maintainer's own branches carry no role.

```bash
gh pr list --repo Danathar/atomic-image-builder --state all --limit 1000 --json headRefName \
  --jq '[.[] | .headRefName | split("/")[0]] | group_by(.) | map({prefix: .[0], count: length})'
```

**The commit trailers.** Since 2026-09-26 an agent commit carries a
`Hive-Run:` trailer naming the issue it was run for, with `Hive-Plan:` and
`Hive-Spec:` beside it. These survive a squash and a rebase where the branch
name does not, and `git log` can filter on them without the GitHub API.

```bash
git log --format='%h %(trailers:key=Hive-Run,valueonly)' | awk 'NF == 2'
git log --format='%h %s' --grep='^Hive-Run: Danathar/atomic-image-builder#<issue>$'
```

The link back to the issue is the fourth mark, and the only one the
contributing guide already asks of everyone: a `Closes #<n>` line in the body,
which GitHub reads into the pull request's closing references.

## What the ledgers hold

Each dated file below is one reading of those marks over a pinned range of
pull requests and commits, with the exact command under every number so it
reproduces, and it is left as it was read. A ledger does not describe current
state, so it does not go stale; for the current picture, run the commands
above.

This is the same shape as [`docs/metrics/`](../metrics/), for the same reason.
It is not a fourth learning artifact beside the three in
[`docs/reflections/`](../reflections/README.md): those record what was learned,
this records who did what.

## Ledgers

- [2026-10-03](2026-10-03.md) — pull requests up to #623, `main` at `fb0e6ff`

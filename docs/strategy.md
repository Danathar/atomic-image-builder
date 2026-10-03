# Strategy

Where this project is trying to get to, how far away it is, and whether the
work being merged is taking it there. Open it before deciding what to work on
next, or before pointing an agent at something.

Like [`docs/metrics.md`](metrics.md), this page quotes no numbers. Each answer
is one command against data the project already keeps, so the page cannot go
out of date: run the command for today's answer. The goal itself is written
down once, in [ROADMAP.md](../ROADMAP.md), and this page reads it rather than
copying it.

## The goal

The project's goal is to leave beta. ROADMAP.md's
[*Current state*](../ROADMAP.md#current-state) sets the bar, and its
[*Beta-exit progress*](../ROADMAP.md#beta-exit-progress) table records each
release against it. The longer-term entries in ROADMAP.md wait until the bar is
met.

The bar counts tagged releases. Merged work moves it only when a release ships
with it, so a long run of merges with no new tag leaves the count where it was.

## Filling in a beta-exit row

One entry per column of the table, in the table's order. `<tag>` is the
release tag, such as `v0.10.0`, and `<date>` is the day it was published, as
`YYYY-MM-DD`. Every search is pinned to that date, so the commands give the
same answer on release day and a month later.

**Release** and **Date**: the newest releases, with the time each was
published.

```bash
gh release list --repo Danathar/atomic-image-builder --limit 5 --json tagName,publishedAt
```

**Containerfile regressions** and **BlueBuild regressions**: every bug issue
opened since the release. Which of them are scan, carry or build regressions,
and which build method each one hit, has to be read from the issue: the bug
form asks for the version but not the build method, and issues an agent files
do not use the form.

```bash
gh issue list --repo Danathar/atomic-image-builder --label bug --state all --limit 1000 \
  --search "created:>=<date>" --json number,title,state
```

**Unit coverage**: the row the coverage history holds for the tagged commit.
Fetch the `coverage-data` branch first, as
[metrics.md](metrics.md#unit-coverage-and-its-history) shows. No output means
no push to `main` ended on that commit; check out the tag and measure it with
the commands in [CONTRIBUTING.md](../CONTRIBUTING.md#coverage) instead. The
second command prints the gate the number has to meet.

```bash
git show origin/coverage-data:coverage-trend.csv | grep "$(git rev-list -n 1 <tag>)"
jq -er '.gated.unit' .coverage-thresholds.json
```

**Open five-stage correctness issues**: bug issues that were still open at the
end of the release day. Count only the ones about a stage
[ARCHITECTURE.md](../ARCHITECTURE.md#the-runtime-model) lists. A bug in the
docs, in CI or in the agents' own tooling is not one.

```bash
gh issue list --repo Danathar/atomic-image-builder --label bug --state all --limit 1000 \
  --search "created:<=<date> -closed:<=<date>" --json number,title
```

**Counts**: no command. ROADMAP.md says how a row's other cells decide it.

## Is the work going there?

**What has merged since the newest release**, grouped by branch prefix. Here
`<date>` is the newest release's date. The prefix is the nearest thing to
"what kind of work" the history records.
[`docs/agent-tasks/`](agent-tasks/README.md) says which agent uses which
prefix, and how to trace a pull request back to its issue.

```bash
gh pr list --repo Danathar/atomic-image-builder --state merged --limit 1000 \
  --search "merged:>=<date>" --json headRefName \
  --jq 'map(.headRefName | split("/")[0]) | group_by(.) | map({prefix: .[0], count: length})'
```

The bar is about the tool's runtime: what a user runs, what it generates, and
whether that image builds. Work on the agents' guardrails, on CI or on this
repository's own process does not move it, however much of it there is. That
work may still be worth doing. But when this count is large and the progress
count in ROADMAP.md has not moved, this is the question to ask.

**What is waiting on the maintainer**: issues an agent stopped on because the
next step is a decision only the maintainer can make. Nothing under one moves
until a person decides and removes the label.

```bash
gh issue list --repo Danathar/atomic-image-builder --label needs-decision --state open --limit 1000
```

## Why there is no report job

This page is commands, not a scheduled job that writes a report, for the
reason [metrics.md](metrics.md#what-these-numbers-do-not-mean) gives for its
own numbers: a job adds a moving part whose output nobody reads weekly. The
beta-exit table is filled in by hand once per release, and that is the pace
the goal moves at.

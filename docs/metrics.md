# Metrics

What this project measures, where each number lives, and how to reproduce it.

Read the last section before quoting any of these. Two of them look like
quality scores and are not, and this repo has already filed one of its own
metrics as a bug ([#123](https://github.com/Danathar/atomic-image-builder/issues/123))
by reading it that way.

## Unit coverage, and its history

The gated one. Current value is on the README badge; the history is a CSV on
the `coverage-data` branch, one row per push to `main`, written by
`coverage_badge.py` in `ci.yml`'s `publish-coverage` job.

```bash
git fetch origin coverage-data:refs/remotes/origin/coverage-data
git show origin/coverage-data:coverage-trend.csv     # date, sha, percent
```

The gate itself comes from `.coverage-thresholds.json`, not from the workflow.
[CONTRIBUTING.md](../CONTRIBUTING.md#coverage) describes all five coverage
measurements and why only this one is gated.

## The other four coverage tiers

End-to-end, shell-entrypoint, maintenance-audit and homebrew-release coverage
are uploaded as workflow artifacts and expire after 30 days. They are
advisory, and three of them are *supposed* to read low. Their numbers are not
tracked over time on purpose: a trend line invites treating them as targets,
which for the maintenance-audit tier would mean manufacturing the live
failures it exists to observe.

```bash
gh run download <run-id>       # coverage-unit, coverage-shell, coverage-e2e
```

## Pull request throughput

```bash
gh pr list --state all --limit 200 --json state \
  --jq 'group_by(.state)|map({state:.[0].state,count:length})'
```

As of 2026-09-03: 112 merged, 1 closed, 3 open.

## Review findings per pull request

The one number here with real signal, because it counts problems found rather
than work completed. `chatgpt-codex-connector` reviews pull requests and posts
findings as inline review comments, which `gh pr view` does not show:

```bash
gh api repos/Danathar/atomic-image-builder/pulls/<N>/comments \
  --jq '[.[]|select(.user.login=="chatgpt-codex-connector[bot]")]|length'
```

Across the most recent 40 pull requests, 11 carried at least one finding, and
the highest single count was 4. Every one of those was verified against the
code before being acted on; several were correct, and the record of the ones
that changed a decision is in
[`.claude/memory/corrections.md`](../.claude/memory/corrections.md).

## Weekly audit outcomes

`maintenance-audit.yml` runs Mondays and distinguishes failures from
advisories. Its run history is the record:

```bash
gh run list --workflow maintenance-audit.yml --limit 20
```

A red run here is not automatically a defect. See
[MAINTAINER.md](../maintainer_docs/MAINTAINER.md)'s *Reading the weekly audit*,
and note the job was deliberately changed to stop going red every week
([#129](https://github.com/Danathar/atomic-image-builder/issues/129)).

## What these numbers do not mean

**Merge rate is not an acceptance rate.** Nearly every pull request here is
opened by the maintainer or on the maintainer's instruction, so ~99% merged
measures who opens them, not whether they were any good. It would read the
same on a project merging everything unreviewed. Do not quote it as a quality
figure.

**100% unit coverage is not 100% tested.** The trend CSV has been flat at 100
for its whole span, which says the gate is never the binding constraint, not
that nothing is untested. It measures the source tree under mocks; what a real
run of the built image executes is the end-to-end tier, and that sits near
10%. Both facts are true at once and the second is the more informative one.

**A low advisory number is often the correct reading.** This is the specific
mistake already made and documented here. The maintenance-audit tier is low
because a clean live run cannot reach a rate limit or a DNS failure, and
driving it up would destroy the only signal it carries.

**None of this is collected on a schedule**, and deliberately so. Every number
above is one command against data the project already produces. A dashboard
job would add a moving part whose output nobody reads weekly, and the two
things actually worth watching -- the coverage gate and the audit -- already
fail loudly on their own.
